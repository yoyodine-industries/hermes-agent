"""Dispatcher: crash/stale/orphan detection, failure accounting and the respawn circuit breaker, memory-aware concurrency caps, the one-shot ``dispatch_once`` pass, worker spawning (``_default_spawn``), worker-log rotation and the long-lived ``run_daemon`` loop.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Mapping
from typing import Optional
from typing import TYPE_CHECKING

from hermes_cli import kanban_crash_sweep_guard as _crash_guard
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

if TYPE_CHECKING:
    from hermes_cli.kanban_db import Task

# Module logger. The witness/contract code (restored from the 2026-10-10 autostash)
# announces the liveness contract through it; the rest of this module logs through ``_kb._log``.
logger = logging.getLogger(__name__)


# After this many consecutive non-success attempts on a task/profile the
# dispatcher parks the task in ``blocked`` with a reason — prevents retry storms.
DEFAULT_FAILURE_LIMIT = 2

# Worker log files larger than this at spawn time are rotated.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and make a terminal board call (kanban_block/kanban_complete/kanban_request_review)
# before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30
# Slot recycling (ruling t_b2865b89 §4): ``kanban.default_max_runtime_seconds`` — the cap
# stamped onto a card that carries none AT CLAIM TIME, so ``enforce_max_runtime`` can always
# reap the slot instead of leaving the tick budget at 0 for hours. ``0`` disables the stamp.
DEFAULT_MAX_RUNTIME_SECONDS = 7200

# A healthy worker is still alive for a while after kanban_complete /
# kanban_request_review returns (final assistant turn, session persistence), so
# a run's retained worker is only reaped once ended_at is at least this old
# (two default dispatch ticks).
TERMINAL_WORKER_REAP_GRACE_SECONDS = 120

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
# The auth family is a curated list, not an open `auth\w*` stem: that stem
# also matched ordinary English words like "author"/"authored"/"authoring"/
# "authoritative" in worker progress prose, parking a healthy card forever
# (#117009).
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|"
    r"auth|authenticat(?:e|es|ed|ing|ion)|authoriz(?:e|es|ed|ing|ation)|"
    r"authoris(?:e|es|ed|ing|ation)|authz|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Cooldown after a rate-limited (quota-wall) requeue before re-spawning. Without
# it the task would re-spawn on the very next tick and bounce off the same quota
# wall, burning a worker slot every tick for hours. Overridable via
# ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

# Cooldown after a storage-locked (state-store contention) requeue before
# re-spawning. Default 0 is DELIBERATE and is the contract's "immediately": the
# reclaim pass already re-queued the card on this tick and the dispatcher's own
# 60 s cadence is the spacing, so nothing is consumed and there is no hot loop.
# The env override (``HERMES_KANBAN_STORAGE_LOCKED_COOLDOWN_SECONDS``) exists for
# tests and for an operator who wants wider spacing around a known maintenance
# window.
#
# Landed on the reconciled line (card t_3286715e) because committed
# ``hermes_cli/kanban_db.py`` imports this name (config default for
# ``_resolve_storage_locked_cooldown_seconds``) while the constant itself lived
# only in the live install's UNCOMMITTED dispatch file: a fresh clone of either
# line could not import ``hermes_cli.kanban_db`` at all. The consumer half of
# the feature (the storage-locked plumbing in the dispatcher) is still owed by
# its own card, t_ccfb92a1 -- this constant alone changes no behaviour.
DEFAULT_STORAGE_LOCKED_COOLDOWN_SECONDS = 0

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Spawn-seam substrate import guard (card t_a2f35c4d, ruling t_dbf5c876)
# ---------------------------------------------------------------------------
#
# Measured incident (2026-10-10): every review-run worker spawn on the fleet died with
# ``cannot import name 'ADVISORY_SKILLS_ENV' from 'agent.skill_commands'`` -- an import no
# source file contained -- and the whole review LANE went silently dead. The stale
# ``cpython-311`` bytecode beside the tree was a smell, not the wound: CPython selects
# ``__pycache__/<mod>.<sys.implementation.cache_tag>.pyc``, so a 3.14 runtime cannot load a
# 3.11 pyc at all (measured: 733 inert foreign-tag files against 1594 live ones, 0 sourceless,
# 0 hash-based). The wound is a live tree whose OWN modules disagree -- an importer present,
# its provider absent -- so the durable half is to NAME that artifact at the spawn seam and to
# classify it as SUBSTRATE: the card spends nothing of its retry budget, and one signature-
# deduped lane actor is filed instead of the lane dying quietly.
#
# Boundary (deliberate, ruling D1): nothing here refuses a spawn on foreign-tag bytecode. A
# foreign-tag population is inert, and a refusal would be a false-positive spawn killer -- the
# exact failure mode this guard exists to remove. The sweep helper REPORTS the two layouts that
# can change behaviour (a sourceless ``module.pyc``, a non-zero header flags field) and measures
# the foreign-tag population as inert.

#: Greppable prefix of the one-line naming payload (D4). It leads, because
#: ``_record_task_failure`` truncates the stored error at 500 chars.
TREE_IMPORT_INCONSISTENCY = "tree-import-inconsistency:"

#: Classifier verdicts (D5). ``card`` is the default and never widens on its own.
SPAWN_FAILURE_CARD = "card"
SPAWN_FAILURE_SUBSTRATE_IMPORT = "substrate_import"
SPAWN_FAILURE_HOST_CAPACITY = "host_capacity"
#: The card's worktree anchor is not a git repository (``WorktreeAnchorNotRepo``). NOT
#: infrastructure and NOT retryable: the wiring is the card's, and a plain folder does not
#: become a repo between tick N and tick N+1. The card is blocked on the FIRST such failure and
#: annotated with ``spawn_failed: no repo at <path>`` plus the repair (card t_e5ff985b).
SPAWN_FAILURE_WORKSPACE_NOT_REPO = "workspace_not_repo"
#: A workspace path component is a symlink whose target is gone (``WorkspaceVolumeUnavailable``):
#: the host's volume is not mounted. INFRASTRUCTURE and RETRYABLE -- nothing of the card ran, the
#: repair is on the host, and the run is closed as a DEFERRAL so the card keeps its retry budget
#: and is claimable again on the first tick after the volume returns (ruling t_f2c8b3fe §4).
#: The value doubles as the event kind the deferral is recorded under.
SPAWN_FAILURE_WORKSPACE_VOLUME = "workspace_volume_unavailable"

#: ``substrate_import:<provider-relpath>:<symbol>`` -- the lane-level dedupe key (D6).
SUBSTRATE_SIGNATURE_PREFIX = "substrate_import:"

#: The LANE actor: one card per (signature, board, window), addressed to the domain's design
#: authority. The idempotency key is what makes N failures across N cards resolve to ONE card.
SUBSTRATE_ACTOR_IDEMPOTENCY_PREFIX = "substrate:"
SUBSTRATE_ACTOR_ASSIGNEE = "platform-stl"
SUBSTRATE_ACTOR_EVENT = "substrate_actor_filed"
SUBSTRATE_ACTOR_TITLE = "[guard] substrate import failure in the dispatching tree"

#: ``from x import y`` -- the shape whose ``y`` is the symbol the naming line quotes.
_CANNOT_IMPORT_NAME_RE = re.compile(r"cannot import name ['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]")

#: A ``def`` / ``class`` / assignment binding of a name: the ``defines-it`` HINT (D4 says a hint,
#: never authority -- the verdict that matters is the interpreter's, and that is the exception).
_SYMBOL_BINDING_RE_TEMPLATE = (
    r"(?m)^[ \t]*(?:async[ \t]+def|def|class)[ \t]+{name}\b"
    r"|^[ \t]*{name}[ \t]*(?::[^=\n]*)?=(?!=)"
    r"|^[ \t]*{name}[ \t]*(?:,|\)|\]|\}})"
)


def _dispatching_tree_root() -> Path:
    """The tree THIS dispatcher runs from -- the closure root the guard reasons about.

    Derived from this module's own file, which is the same directory the tree-skew fence
    resolves for itself; ``kanban_db_dispatch`` deliberately does not import that fence (a
    sibling suite pins the absence of that dependency), so the root is spelled out here rather
    than delegated. Every guard helper takes ``tree`` explicitly and the dispatch seam reads the
    default from THIS one function -- which is the seam a test patches instead of breaking the
    real checkout.
    """
    return Path(__file__).resolve().parents[1]


def _pyc_tag(filename: str) -> Optional[str]:
    """The interpreter tag of a bytecode filename: ``mod.cpython-311.pyc`` -> ``cpython-311``.

    ``mod.pyc`` (untagged) -> None. The tag is the LAST dot-separated component before ``.pyc``,
    because a tag itself carries a hyphen -- splitting on ``-`` would mangle it.
    """
    name = str(filename)
    if not name.endswith(".pyc"):
        return None
    stem = name[: -len(".pyc")]
    if "." not in stem:
        return None
    return stem.rsplit(".", 1)[1] or None


def _pyc_header_flags(raw: bytes) -> int:
    """The 4-byte header flags field of a ``.pyc`` (0 when too short to carry one).

    Header layout: 4-byte magic, 4-byte flags, then either (mtime, size) or an 8-byte hash.
    """
    if len(raw) < 8:
        return 0
    return int.from_bytes(raw[4:8], "little")


def sweep_tree_bytecode_pycs(tree: Path) -> dict[str, Any]:
    """REPORT the bytecode layouts under ``tree`` that can change behaviour -- and those that cannot.

    ``sourceless``: an ``*.pyc`` OUTSIDE ``__pycache__`` with no ``*.py`` beside it -- the import
    system will happily load it, so it can shadow source that a checkout thinks it has.
    ``flags_nonzero`` / ``hash_based``: a header whose flags field is non-zero (a
    hash-invalidated pyc, whose staleness is judged by content rather than mtime).
    ``current_tag`` / ``foreign_tag`` / ``untagged``: the tag population. A FOREIGN tag is inert
    -- this interpreter can never select it -- which is why this function only ever reports and
    no spawn is gated on the result (ruling D1).
    """
    tree = Path(tree)
    cache_tag = sys.implementation.cache_tag
    report: dict[str, Any] = {
        "tree": str(tree),
        "cache_tag": cache_tag,
        "sourceless": [],
        "current_tag": 0,
        "foreign_tag": 0,
        "untagged": 0,
        "flags_nonzero": 0,
        "hash_based": 0,
        "pyc_total": 0,
    }
    try:
        paths = sorted(tree.rglob("*.pyc"))
    except OSError:
        return report
    for path in paths:
        report["pyc_total"] += 1
        tag = _pyc_tag(path.name)
        if tag == cache_tag:
            report["current_tag"] += 1
        elif tag is None:
            report["untagged"] += 1
        else:
            report["foreign_tag"] += 1
        if "__pycache__" not in path.parts and not path.with_suffix(".py").exists():
            try:
                rel = path.relative_to(tree).as_posix()
            except ValueError:
                rel = str(path)
            report["sourceless"].append(rel)
        try:
            flags = _pyc_header_flags(path.read_bytes())
        except OSError:
            continue
        if flags:
            report["flags_nonzero"] += 1
            if flags & 0b11:
                report["hash_based"] += 1
    return report


def _is_under(path: Any, tree: Path) -> bool:
    """Does ``path`` resolve inside ``tree``? The guard's whole "this tree's own" test."""
    try:
        Path(path).resolve().relative_to(Path(tree).resolve())
        return True
    except (OSError, TypeError, ValueError):
        return False


def _relative_to_tree(path: Any, tree: Path) -> str:
    """``path`` spelled relative to ``tree`` (posix), or its absolute spelling when it escapes."""
    try:
        return Path(path).resolve().relative_to(Path(tree).resolve()).as_posix()
    except (OSError, TypeError, ValueError):
        return str(path)


def _importer_frame(exc: BaseException, tree: Path) -> Optional[tuple[str, int]]:
    """Deepest traceback frame whose file lives under ``tree``: ``(relpath, lineno)`` or None.

    "Deepest" is the frame that RAISED -- the importer whose ``from ... import`` disagreed. The
    interpreter's own ``importlib`` frames are skipped by the under-tree test, and a failure whose
    only frames are outside the tree is by definition not the tree's (the card's, the test's, or
    a third-party module's).
    """
    try:
        frames = traceback.extract_tb(getattr(exc, "__traceback__", None))
    except Exception:
        return None
    for frame in reversed(frames):
        if _is_under(frame.filename, tree):
            return _relative_to_tree(frame.filename, tree), int(frame.lineno or 0)
    return None


def _imported_symbol(exc: BaseException) -> Optional[str]:
    """The name the importer asked for: ``cannot import name 'X' from 'Y'`` -> ``X``.

    Only that shape carries a symbol, and a symbol is what the signature and the naming line are
    built from -- a bare ``No module named 'x'`` has none, so it cannot be deduped and stays the
    card's (deliberately never widened).
    """
    match = _CANNOT_IMPORT_NAME_RE.search(str(exc))
    return match.group(1) if match else None


def _provider_source(exc: BaseException) -> Optional[Path]:
    """The provider module's SOURCE FILE, resolved from its spec -- the spec is never imported.

    ``importlib.util.find_spec`` answers where the name would load from without executing it, so
    reporting a broken provider cannot itself run the broken module. A spec that is absent
    (a missing third-party dependency) or whose origin is not a ``.py`` file (a builtin, a frozen
    module, an extension, a namespace package) is not a source this guard speaks about -> None.
    """
    name = getattr(exc, "name", None)
    origin: Any = None
    if name:
        try:
            spec = importlib.util.find_spec(name)
        except Exception:
            spec = None
        origin = getattr(spec, "origin", None) if spec is not None else None
    if not origin:
        raw_path = getattr(exc, "path", None)
        origin = raw_path if isinstance(raw_path, (str, os.PathLike)) else None
    if not origin:
        return None
    path = Path(origin)
    return path if path.suffix == ".py" else None


def _source_defines_symbol(text: str, symbol: str) -> bool:
    """Does this provider source BIND ``symbol``? A hint for the naming line, never authority."""
    return re.search(
        _SYMBOL_BINDING_RE_TEMPLATE.format(name=re.escape(symbol)), text,
    ) is not None


def _head_verdict(tree: Path, relpath: str, on_disk_sha256: str) -> Optional[bool]:
    """Is the on-disk file different from ``HEAD:<relpath>``? ``None`` when git cannot answer.

    The exit status is checked FIRST: a failed ``git show`` piped into a hash yields the
    EMPTY-INPUT digest (``e3b0c442...``), which would report a difference that does not exist
    (ruling D4). A tree git cannot answer for reports unknown, never a verdict.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(tree), "show", f"HEAD:{relpath}"],
            capture_output=True,
        )
    except (OSError, ValueError):
        return None
    if proc.returncode != 0:
        return None
    return hashlib.sha256(proc.stdout).hexdigest() != on_disk_sha256


def _foreign_tag_pyc_count(provider: Path) -> int:
    """Bytecode files beside the provider carrying a tag THIS interpreter cannot select."""
    cache = provider.parent / "__pycache__"
    try:
        entries = list(cache.glob("*.pyc"))
    except OSError:
        return 0
    cache_tag = sys.implementation.cache_tag
    return sum(1 for entry in entries if _pyc_tag(entry.name) not in (None, cache_tag))


def _tree_import_evidence(exc: BaseException, tree: Path) -> Optional[dict[str, Any]]:
    """What the naming line quotes about a tree-internal import disagreement, or None.

    None means "not this tree's own disagreement", and it is the ONLY place that decision is
    made: BOTH halves must hold -- an ``ImportError`` carrying a symbol, whose deepest under-tree
    frame raised it, whose provider resolves to a source file under the same tree. A missing
    third-party dependency (no resolvable spec, or one outside the tree) therefore stays on the
    card. This runs on the FAILURE path only; the happy path walks no import closure (D4).
    """
    if not isinstance(exc, ImportError):
        return None
    importer = _importer_frame(exc, tree)
    if importer is None:
        return None
    provider = _provider_source(exc)
    if provider is None or not _is_under(provider, tree):
        return None
    symbol = _imported_symbol(exc)
    if not symbol:
        return None
    try:
        raw = provider.read_bytes()
        stat = provider.stat()
    except OSError:
        return None
    sha256 = hashlib.sha256(raw).hexdigest()
    provider_rel = _relative_to_tree(provider, tree)
    defines_it = _source_defines_symbol(raw.decode("utf-8", "replace"), symbol)
    return {
        "importer_rel": importer[0],
        "importer_line": importer[1],
        "symbol": symbol,
        "provider_rel": provider_rel,
        "provider_sha256": sha256,
        "provider_mtime": int(stat.st_mtime),
        "provider_size": int(stat.st_size),
        "defines_it": defines_it,
        "differs_from_head": _head_verdict(tree, provider_rel, sha256),
        "foreign_tag_pyc": _foreign_tag_pyc_count(provider),
    }


def describe_tree_import_failure(
    exc: BaseException, *, tree: Optional[Path] = None, task_id: Optional[str] = None,
) -> Optional[str]:
    """ONE line naming the artifact, front-loaded for the 500-char truncation (D4), else None.

    Shape::

        tree-import-inconsistency: importer <relpath>:<lineno> imports <symbol>; provider
        <relpath> sha256=<64> mtime=<epoch> size=<n> defines-it=no differs-from-HEAD=yes;
        tree-self-consistent=no; foreign-tag-pyc-in-provider-dir=0; nothing of card <id> ran

    ``tree-self-consistent`` is the tree's own verdict: this closure has exactly one witness --
    the provider's current source -- so it reads the same fact as ``defines-it``, and it is
    carried separately because it is what the reader greps for ("are this tree's modules
    agreeing?"). ``differs-from-HEAD`` says whether the broken provider is the committed bytes,
    which is what separates "someone edited this tree" from "the tree shipped broken".
    """
    tree = Path(tree) if tree is not None else _dispatching_tree_root()
    evidence = _tree_import_evidence(exc, tree)
    if evidence is None:
        return None
    head = evidence["differs_from_head"]
    head_text = "unknown" if head is None else ("yes" if head else "no")
    self_consistent = "yes" if evidence["defines_it"] else "no"
    return (
        f"{TREE_IMPORT_INCONSISTENCY} importer {evidence['importer_rel']}:{evidence['importer_line']}"
        f" imports {evidence['symbol']}; provider {evidence['provider_rel']}"
        f" sha256={evidence['provider_sha256']} mtime={evidence['provider_mtime']}"
        f" size={evidence['provider_size']} defines-it={'yes' if evidence['defines_it'] else 'no'}"
        f" differs-from-HEAD={head_text}; tree-self-consistent={self_consistent};"
        f" foreign-tag-pyc-in-provider-dir={evidence['foreign_tag_pyc']};"
        f" nothing of card {task_id or 'unknown'} ran"
    )


def substrate_import_signature(
    exc: BaseException, *, tree: Optional[Path] = None,
) -> Optional[str]:
    """``substrate_import:<provider-relpath>:<symbol>`` -- the lane-level dedupe key (D6), or None."""
    tree = Path(tree) if tree is not None else _dispatching_tree_root()
    evidence = _tree_import_evidence(exc, tree)
    if evidence is None:
        return None
    return f"{SUBSTRATE_SIGNATURE_PREFIX}{evidence['provider_rel']}:{evidence['symbol']}"


def classify_spawn_failure(exc: BaseException, *, tree: Optional[Path] = None) -> str:
    """The ONE decider for who owns a failed spawn: ``card`` | ``substrate_import`` | ``host_capacity``.

    * ``host_capacity`` -- the host refused to place the worker (``RestartSafeScopeUnavailable``):
      neither the card's failure nor the tree's, retried spaced and never charged.
    * ``substrate_import`` -- THIS tree's own modules disagree: the importer is source under the
      tree and its provider is source under the tree that does not supply the name. Nothing of
      the card ran, so the failure must not spend the card's retry budget, and it must be counted
      for the LANE actor instead of parking the card.
    * ``workspace_volume_unavailable`` -- a workspace path component is a symlink whose target is
      gone (``WorkspaceVolumeUnavailable``): the host's volume is not mounted. Nothing of the card
      ran and no retry of the card can mount a volume, so this is infrastructure: the run closes as
      a deferral and the card keeps its retry budget. It is checked BEFORE the no-repo verdict,
      because with a volume gone every anchor also answers "not a repo" -- and that verdict blocks
      the card on the first attempt.
    * ``workspace_not_repo`` -- the card's own worktree anchor is not a git repository
      (``WorktreeAnchorNotRepo``). The card's wiring is wrong and no retry can fix a folder, so
      this is the one card-class verdict that trips the breaker on the FIRST failure.
    * ``card`` -- everything else, deliberately including a missing third-party dependency and a
      failure whose frames are all outside the tree. The classifier never widens its own reach:
      an import failure it cannot attribute to THIS tree stays the card's.

    Callers must not scatter ``isinstance`` checks beside this: one verdict per failure.
    """
    from hermes_cli.kanban_db_workspace import WorkspaceVolumeUnavailable, WorktreeAnchorNotRepo

    if isinstance(exc, WorkspaceVolumeUnavailable):
        return SPAWN_FAILURE_WORKSPACE_VOLUME
    if isinstance(exc, WorktreeAnchorNotRepo):
        return SPAWN_FAILURE_WORKSPACE_NOT_REPO
    try:
        from tools.process_registry import RestartSafeScopeUnavailable
    except Exception:  # pragma: no cover - a host without the module has no such refusal
        RestartSafeScopeUnavailable = ()  # type: ignore[assignment]
    if RestartSafeScopeUnavailable and isinstance(exc, RestartSafeScopeUnavailable):
        return SPAWN_FAILURE_HOST_CAPACITY
    tree = Path(tree) if tree is not None else _dispatching_tree_root()
    if _tree_import_evidence(exc, tree) is not None:
        return SPAWN_FAILURE_SUBSTRATE_IMPORT
    return SPAWN_FAILURE_CARD


def _spawn_failure_report(
    exc: BaseException, task_id: str, *, tree: Optional[Path] = None,
) -> tuple[str, str, Optional[dict[str, Any]]]:
    """``(error_text, kind, metadata_extra)`` for one failed spawn -- the seam's single classify call.

    ``error_text`` is the naming line when this is the tree's own import disagreement (it LEADS,
    because the stored error is cut at 500 chars), else the exception's own text. ``metadata_extra``
    rides the run and the event so the lane actor can dedupe on the signature without re-deriving
    it from prose later.
    """
    tree = Path(tree) if tree is not None else _dispatching_tree_root()
    kind = classify_spawn_failure(exc, tree=tree)
    if kind == SPAWN_FAILURE_WORKSPACE_VOLUME:
        # The refusal text already LEADS with the marker, and the component names the unmounted
        # volume both on the run and on the event -- a deferral has to say WHICH mount to fix.
        return str(exc), kind, {
            "workspace_volume_unavailable": True,
            "workspace_volume_component": getattr(exc, "component", None),
        }
    if kind != SPAWN_FAILURE_SUBSTRATE_IMPORT:
        return str(exc), kind, None
    metadata_extra: dict[str, Any] = {"substrate_import": True}
    signature = substrate_import_signature(exc, tree=tree)
    if signature:
        metadata_extra["substrate_signature"] = signature
    named = describe_tree_import_failure(exc, tree=tree, task_id=task_id)
    return (named or str(exc)), kind, metadata_extra


def _substrate_failure_threshold() -> int:
    """``kanban.failure_threshold`` (legacy alias ``spawn_failure_threshold``) else the default.

    NO new config key (D5): the lane actor's bar is the same one the per-card breaker uses, so an
    operator who already tuned ``failure_threshold`` tunes this too. A non-positive or
    unparseable value falls back to ``DEFAULT_FAILURE_LIMIT``.
    """
    try:
        from hermes_cli.config import load_config_readonly

        kanban_cfg = (load_config_readonly() or {}).get("kanban") or {}
    except Exception:
        return DEFAULT_FAILURE_LIMIT
    if not isinstance(kanban_cfg, dict):
        return DEFAULT_FAILURE_LIMIT
    raw = kanban_cfg.get("failure_threshold", kanban_cfg.get("spawn_failure_threshold"))
    if raw is None:
        return DEFAULT_FAILURE_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_FAILURE_LIMIT
    return value if value >= 1 else DEFAULT_FAILURE_LIMIT


def _substrate_window(now: int) -> tuple[int, int]:
    """``(window_start, window_seconds)`` for the lane actor's idempotency key.

    The window is the SAME cooldown the retired card is spaced by
    (``_resolve_rate_limit_cooldown_seconds``), bucketed so every failure inside one window
    resolves to one key -- that bucketing is what makes the actor idempotent per tick. A disabled
    cooldown (0, the tests' switch) still needs a bucket, so it falls back to the default.
    """
    seconds = _kb._resolve_rate_limit_cooldown_seconds()
    if seconds <= 0:
        seconds = DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS
    now = int(now)
    return (now // seconds) * seconds, seconds


def _substrate_affected_cards(
    conn: sqlite3.Connection, signature: str, *, window_start: int,
) -> list[str]:
    """DISTINCT cards whose recorded substrate failure carries ``signature`` in this window.

    Read from the board's own events: the ``spawn_failed`` row the seam writes carries
    ``substrate_signature`` (``_record_task_failure(..., metadata_extra=...)``), so the count is
    per BOARD with no new table and no new config key. Distinct CARDS, not failures: the actor
    exists because a LANE is dying, and one hot card retrying is the per-card ``gave_up``'s story.
    """
    affected: list[str] = []
    for row in conn.execute(
        "SELECT task_id, payload FROM task_events "
        "WHERE kind = 'spawn_failed' AND created_at >= ? ORDER BY created_at",
        (int(window_start),),
    ).fetchall():
        payload = _kb._json_dict(row["payload"])
        if payload.get("substrate_signature") != signature:
            continue
        task_id = row["task_id"]
        if task_id not in affected:
            affected.append(task_id)
    return affected


def _substrate_provider_path(tree: Path, signature: str) -> Optional[Path]:
    """The provider file a signature names: ``substrate_import:<relpath>:<symbol>`` -> path."""
    body = signature[len(SUBSTRATE_SIGNATURE_PREFIX):]
    if ":" not in body:
        return None
    relpath = body.rsplit(":", 1)[0]
    if not relpath:
        return None
    return Path(tree) / relpath


def _recorded_naming_line(conn: sqlite3.Connection, affected: list[str]) -> Optional[str]:
    """The naming line as RECORDED on one affected card -- quoted verbatim in the actor's body."""
    for task_id in affected:
        row = conn.execute(
            "SELECT last_failure_error FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        text = _kb._lossy_text(row["last_failure_error"]) if row is not None else None
        if text and TREE_IMPORT_INCONSISTENCY in text:
            return text
    return None


def _substrate_actor_body(
    conn: sqlite3.Connection, signature: str, affected: list[str], tree: Path,
) -> str:
    """The actor card's body: the naming line, the affected cards, and the tree evidence (D6).

    Deterministic -- no model, no prose a human has to interpret. What a reader needs is: which
    module disagreeing with which symbol, which cards died on it, and the tree's own state.
    """
    window_start, window_seconds = _substrate_window(int(time.time()))
    lines = [
        "## Observed",
        "",
        f"`{TREE_IMPORT_INCONSISTENCY}` the dispatching tree's own modules disagree, so worker",
        "spawns are dying at the seam. Filed by the dispatcher's spawn-seam substrate guard --",
        "deterministic, no model was consulted.",
        "",
        f"- signature: `{signature}`",
        f"- affected cards ({len(affected)}): " + ", ".join(f"`{tid}`" for tid in affected),
        f"- window: {window_start} (+{window_seconds}s)",
        "",
        "## Recorded failure line",
        "",
        "```",
        _recorded_naming_line(conn, affected) or f"{TREE_IMPORT_INCONSISTENCY} {signature}",
        "```",
        "",
        "## Tree evidence",
        "",
        f"- tree: `{tree}`",
    ]
    provider = _substrate_provider_path(tree, signature)
    if provider is not None and provider.is_file():
        try:
            raw = provider.read_bytes()
            stat = provider.stat()
        except OSError:
            raw, stat = b"", None
        if stat is not None:
            lines.append(
                f"- provider: `{_relative_to_tree(provider, tree)}`"
                f" sha256={hashlib.sha256(raw).hexdigest()}"
                f" mtime={int(stat.st_mtime)} size={int(stat.st_size)}"
            )
    else:
        lines.append(f"- provider: `{_substrate_provider_path(tree, signature)}` (not readable)")
    lines += ["", "```", *_git_status_lines(tree), "```"]
    return "\n".join(lines)


def _git_status_lines(tree: Path) -> list[str]:
    """``git status --porcelain`` for the tree, or the reason it could not be read."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(tree), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError) as exc:
        return [f"(git unavailable: {exc})"]
    if proc.returncode != 0:
        return [f"(git status failed rc={proc.returncode}: {proc.stderr.strip()})"]
    return proc.stdout.splitlines() or ["(clean)"]


def _board_key(conn: sqlite3.Connection, board: Optional[str]) -> str:
    """The board slug the actor's idempotency key is scoped to.

    The CONNECTION is the board: ``PRAGMA database_list`` names the DB file it writes, and the
    directory holding it IS the slug (``<kanban_home>/boards/<slug>/kanban.db``). An explicit
    ``board`` argument wins when the caller carries one, and the current-board helper is the last
    fallback. Only upstream helpers are used: an override-local one would make the key's spelling
    depend on whatever else the tree happens to carry.
    """
    if board:
        try:
            explicit = _kb._normalize_board_slug(board)
        except Exception:
            explicit = None
        if explicit:
            return explicit
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if str(row[1]) == "main" and row[2]:
                db_file = Path(str(row[2]))
                if db_file.parent == Path(_kb.kanban_home()):
                    return "default"      # the default board's DB sits at <kanban_home>/kanban.db
                slug = db_file.parent.name
                if slug:
                    return slug
    except Exception:
        pass
    try:
        return _kb._slug_or_default(board)
    except Exception:
        return "default"


def _file_substrate_actor(
    conn: sqlite3.Connection,
    signature: str,
    *,
    affected: list[str],
    tree: Path,
    board: Optional[str] = None,
) -> Optional[str]:
    """File EXACTLY ONE acting card for a lane-level signature, idempotently (D6).

    The idempotency key (``substrate:<signature>:<board>:<window-start>``) is the whole mechanism:
    N cards failing the same way in one window resolve to one card, and a tree that stays broken
    into the NEXT window can file its own. Best-effort by design -- a board that refuses the write
    must not take the dispatcher tick down with it, and the per-card naming line is already
    recorded either way -- but never silent: the refusal is logged with the key it could not file.
    """
    key = (
        f"{SUBSTRATE_ACTOR_IDEMPOTENCY_PREFIX}{signature}"
        f":{_board_key(conn, board)}:{_substrate_window(int(time.time()))[0]}"
    )
    try:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        actor_id = _kb.create_task(
            conn,
            title=SUBSTRATE_ACTOR_TITLE,
            body=_substrate_actor_body(conn, signature, affected, tree),
            assignee=SUBSTRATE_ACTOR_ASSIGNEE,
            created_by="kanban-dispatcher",
            idempotency_key=key,
        )
        with _kb.write_txn(conn):
            _kb._append_event(
                conn, actor_id, SUBSTRATE_ACTOR_EVENT,
                {"signature": signature, "affected": list(affected),
                 "board": _board_key(conn, board)},
            )
        return actor_id
    except Exception as exc:
        _kb._log.warning(
            "kanban dispatcher: could not file the substrate actor for %s (key=%s): %s",
            signature, key, exc,
        )
        return None


def _maybe_file_substrate_actor(
    conn: sqlite3.Connection,
    signature: Optional[str],
    *,
    tree: Path,
    board: Optional[str] = None,
) -> Optional[str]:
    """File the lane actor once ``kanban.failure_threshold`` cards share one signature (D6).

    A per-card ``blocked`` + ``gave_up`` names a card, never the lane -- which is how the review
    lane went silently dead. This is the lane-level half: count DISTINCT cards carrying the
    signature inside the cooldown window, across the board, and at the threshold file one acting
    card for the design authority. The existing per-card ``gave_up`` is left exactly as it was.
    """
    if not signature:
        return None
    window_start, _ = _substrate_window(int(time.time()))
    affected = _substrate_affected_cards(conn, signature, window_start=window_start)
    if len(affected) < _substrate_failure_threshold():
        return None
    return _file_substrate_actor(
        conn, signature, affected=affected, tree=tree, board=board,
    )


def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    exc: BaseException,
    *,
    failure_limit: int,
    board: Optional[str] = None,
    prefix: str = "",
) -> bool:
    """Record one failed spawn through the guard: classify, name, charge-or-not, then escalate.

    Returns True when the card was auto-blocked. One helper for BOTH spawn-seam ``except`` sites
    (workspace resolution and the spawn call), so the verdict is reached exactly once, by
    :func:`classify_spawn_failure`, and no caller re-implements the infrastructure rule beside it
    (D5: one named classifier, no scattered ``isinstance`` checks).
    """
    tree = _dispatching_tree_root()
    error_text, kind, metadata_extra = _spawn_failure_report(exc, task_id, tree=tree)
    infrastructure = kind in (
        SPAWN_FAILURE_SUBSTRATE_IMPORT,
        SPAWN_FAILURE_HOST_CAPACITY,
        SPAWN_FAILURE_WORKSPACE_VOLUME,
    )
    # A non-repo anchor is not retryable: blocking on the FIRST failure is the difference
    # between a card that says "this is broken, here is the fix" and one that burns its
    # retry budget and returns to ``ready`` looking unattended (card t_e5ff985b).
    non_retryable = kind == SPAWN_FAILURE_WORKSPACE_NOT_REPO
    if kind == SPAWN_FAILURE_HOST_CAPACITY:
        _kb._log.warning(
            "kanban dispatcher: spawn of %s deferred, host cannot place the worker: %s",
            task_id, exc,
        )
    elif kind == SPAWN_FAILURE_WORKSPACE_VOLUME:
        _kb._log.warning(
            "kanban dispatcher: spawn of %s deferred, the workspace volume is not mounted: %s",
            task_id, error_text,
        )
    elif kind == SPAWN_FAILURE_SUBSTRATE_IMPORT:
        _kb._log.warning(
            "kanban dispatcher: spawn of %s refused, the dispatching tree's own modules "
            "disagree: %s", task_id, error_text,
        )
    elif non_retryable:
        _kb._log.error(
            "kanban dispatcher: %s cannot spawn and no retry can fix it: %s", task_id, error_text,
        )
    # The naming line leads for a substrate failure; an ordinary failure keeps its caller's
    # context label ("workspace: ..."). The no-repo refusal carries its own leading marker, and
    # so does the volume refusal (its marker is what the reviewer greps for on the card).
    if kind in (SPAWN_FAILURE_SUBSTRATE_IMPORT, SPAWN_FAILURE_WORKSPACE_VOLUME) or non_retryable:
        text = error_text
    else:
        text = f"{prefix}{error_text}"
    blocked = _record_task_failure(
        conn, task_id, text,
        outcome="spawn_failed", failure_limit=failure_limit, release_claim=True, end_run=True,
        infrastructure=infrastructure, metadata_extra=metadata_extra, force_trip=non_retryable,
    )
    if kind == SPAWN_FAILURE_WORKSPACE_VOLUME:
        # The DEFERRAL is recorded as itself, not only as a ``spawn_failed`` row: a lane that goes
        # quiet because a volume went away has to say so where the board's own history is read.
        try:
            with _kb.write_txn(conn):
                _kb._append_event(
                    conn, task_id, SPAWN_FAILURE_WORKSPACE_VOLUME,
                    {
                        "deferral": True,
                        "error": error_text,
                        "component": (metadata_extra or {}).get("workspace_volume_component"),
                    },
                )
        except Exception:  # pragma: no cover - annotation is best-effort, the deferral is recorded
            _kb._log.warning(
                "kanban dispatcher: could not annotate %s with its volume deferral", task_id,
            )
    if kind == SPAWN_FAILURE_SUBSTRATE_IMPORT:
        _maybe_file_substrate_actor(
            conn, (metadata_extra or {}).get("substrate_signature"), tree=tree, board=board,
        )
    if non_retryable:
        # The refusal must be readable ON the card, not only in the dispatcher's log: a board
        # listing shows status, and a card that failed to spawn for a reason no retry can fix
        # has to SAY so where the next worker looks (D4).
        try:
            _kb.add_comment(
                conn, task_id, "dispatcher",
                f"{error_text}\n\n"
                f"Blocked on the FIRST attempt: a spawn cannot be retried into a git "
                f"repository. Repair the project's primary repo, or re-anchor this card with "
                f"`hermes kanban retarget {task_id} --workspace worktree:<git-repo>`, then "
                f"unblock it.",
            )
        except Exception as exc:  # pragma: no cover - annotation is best-effort, the block is not
            _kb._log.warning(
                "kanban dispatcher: could not annotate %s with its spawn refusal: %s",
                task_id, exc,
            )
    return blocked


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass.

    ``kanban.default_assignee`` applied this tick before spawning (#27145). Surfaces the auto-assignment to
    telemetry / CLI / dashboard so the operator can see when the dispatcher is acting on the fallback rule
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is ``(task_id, assignee,
    current_running_count)``. NOT an operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so telemetry / dashboards can show "this
    profile is busy" vs
    the board's dispatch lock (issue #35240). A losing dispatcher does no DB writes this tick — the lock
    holder is making progress on the same board. This is the steady-state signal that a single-writer guard
    is
    """

    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    """``running`` cards requeued by :func:`reconcile_orphaned_running` (broken
    claim bookkeeping, dead/gone worker)."""
    reaped_terminal_workers: list[str] = field(default_factory=list)
    """Task ids whose worker outlived its closed run and was terminated by
    :func:`reap_terminal_workers`."""
    priority_demoted: list[tuple[str, int, int]] = field(default_factory=list)
    """The above-tranche repair pass's report (``t_6ce41549``, restored by card ``t_2e107499``):
    ``(task_id, was, now)`` for each row LOWERED into its class ceiling this tick. Empty in the
    steady state — a clean board costs one SELECT — and the durable per-card record is the
    ``priority_demoted`` event the pass writes, so a lane can see its own card was moved instead
    of finding out by order."""
    priority_demote_refused: list[str] = field(default_factory=list)
    """``"<task_id> (<was> -> <now>): <error>"`` for each row the above-tranche repair pass could
    NOT lower this tick. A refusal is NOT a success: the row still carries its over-claim, so it
    is named on the tick's own record and logged at ERROR. Never expected — the pass and the
    storage guard read one predicate (``kanban_db.tranche_entitlement``) — and this bucket is
    what makes a disagreement visible instead of silent."""
    priority_demote_error: Optional[str] = None
    """Set when the above-tranche repair pass itself raised instead of returning. The tick
    CONTINUES and dispatches anyway (2026-10-01: an abort in this pass took the whole ``defcon``
    board's dispatching down for 6.5 h, before the spawn loop was reached), so the failure — not
    the tick — is what this field reports."""
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids with no assignee at all — operator-actionable (usually a
    misfiled task waiting for routing)."""
    auto_assigned_default: list[str] = field(default_factory=list)
    """Unassigned task ids that had ``kanban.default_assignee`` applied this
    tick before spawning, so telemetry/CLI/dashboard can show the dispatcher
    acting on the fallback rule rather than explicit assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids whose assignee names a control-plane lane (e.g. a Claude
    Code terminal like ``orion-cc``), not a Hermes profile. Expected steady-state
    on multi-lane setups, NOT operator-actionable; tracked apart so health
    telemetry can tell "stuck" from "correctly idle"."""
    skipped_self_review: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` review rows parked because the assignee is the
    card's OWN implementer — ``"assignee_is_implementer"`` (spawning it would
    hand the review skill to the bot that wrote the change) or
    ``"implementer_unknown"`` (no implementer provenance, so a distinct reviewer
    cannot be proven). Fail-closed: the row stays in ``review`` until a distinct
    reviewer is named. Needs a human, so it is bucketed apart from the
    "busy, retry later" per-profile deferrals."""
    skipped_top_level_only: list[str] = field(default_factory=list)
    """Ready/review task ids HELD at the run door because the card declares
    ``GATE-TOP-LEVEL-ONLY`` (hermes_cli/kanban_top_level_gate.py; ruling
    t_efa908e8): its whole DoD needs a genuine top-level context and a dispatched
    worker is structurally fenced from it. The card is PARKED ``scheduled``
    (due_at NULL) -- never a silent skip -- and the tick's suppression line names
    the hold, because a silent hold is how a starved board reads as an idle one."""
    substrate_held: list[str] = field(default_factory=list)
    """Task ids HELD by per-unit substrate admission (door 3; card ``t_0ae49984``, design
    ``t_72e9bf03``): the card's own workspace root answered a DENIAL to a real operation, so it
    was not claimed this tick. NOT a failure, NOT a block and NOT a count against the card
    (invariant I1) -- the card simply was not admitted, and the next tick re-probes. Siblings on
    the same board carry on (invariant I2). Counted on the tick's suppression line, because a
    silent hold is how a starved board reads as an idle one."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """``(task_id, assignee, current_running_count)`` deferred because the
    assignee is at ``kanban.max_in_progress_per_profile``. Picked up on a later
    tick; separate bucket so dashboards show "profile busy" vs "stuck"."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    crash_sweep_refused: dict = field(default_factory=dict)
    """The R4 refusal this tick, as a plain dict (``signature``/``count``/``threshold``/
    ``ended_at``/``task_ids``), or ``{}``. Non-empty means the reclaim wrote NOTHING: a sweep that
    would close ``>= kanban_crash_sweep_guard.mass_crash_threshold()`` runs in one tick is
    host-systemic, not N card failures (ruling ``t_63a20c59`` R4)."""
    crash_sweep_held: list[str] = field(default_factory=list)
    """Task ids the reclaim HELD because their recorded identity fingerprint is not comparable with
    the shape this dispatcher would write (R2 ``UNKNOWN -> HOLD`` at row granularity, R6). They were
    neither reclaimed nor counted; their claims are untouched."""
    identity_generation_alerts: list[str] = field(default_factory=list)
    """R6 divergence reasons for this tick (``identity-shape-changed:…``, ``psutil-lost``,
    ``witness-bytes-lost:…``); empty when the deployed identity generation is unchanged. Non-empty
    means the shape the dispatcher WRITES is no longer the one recorded, so a liveness comparison is
    meaningless until the witness bytes are re-landed."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed for no heartbeat within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """``(task_id, reason)`` skipped by the respawn guard: ``"blocker_auth"``
    (quota/auth error — also auto-blocked), ``"recent_success"`` (completed run
    within guard window), ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released to ``ready`` WITHOUT counting
    a failure — a long quota window must never trip the circuit breaker."""
    storage_locked: list[str] = field(default_factory=list)
    """Task ids whose workers were stopped by the profile state DB's write lock
    being held by a peer (``KANBAN_STORAGE_LOCKED_EXIT_CODE``) and were released
    to ``ready`` WITHOUT counting a failure. The VISIBILITY surface for this
    class: a card looping on a wedged store must be readable in the tick
    summary, never silent."""
    skipped_locked: bool = False
    """True when another process held the board's dispatch lock: this tick did
    no DB writes; the lock holder is making progress on the same board."""
    skipped_board_disabled: bool = False
    """True when the tick refused to run AT ALL: the board it resolved to is not
    dispatch-enabled (an estate/rehearsal board, ``board.json`` ``"dispatch":
    false``). Nothing was claimed, promoted, reclaimed or spawned — the refusal
    happens BEFORE the lock, the reclaim phase and every claim, so the spawn path
    is never reached and no card on that board can become lane work (card
    t_17c9c847)."""
    memory_pressure: Optional[str] = None
    """Memory pressure that restricted this tick: ``"critical"`` (no new
    workers), ``"elevated"`` (at most one), ``None`` (no restriction).
    Reclaim/promotion bookkeeping still ran; deferred tasks stay queued."""
    tick_yielded: str = ""
    """Non-empty when this tick REFUSED to serve because the tree this process
    imported no longer matches the checkout on disk (the content-based skew
    gate, ruling ``t_8fed34c8``). The reason string starts with
    ``tree-skew-yield:``. A refused tick reclaims nothing, promotes nothing and
    spawns nothing; it is deliberately NOT ``skipped_locked`` and NOT idle --
    a yielding ticker must never read green at the status surface."""

    spawn_budget_blocked: Optional[str] = None
    """Which cap had already consumed this tick's spawn budget, so no card was
    even attempted: ``"max_spawn"`` (``kanban.max_spawn``, or the board's own
    ``kanban.max_spawn_by_board`` ceiling) or ``"max_in_progress"`` (host-wide).
    ``_tick_spawn_budget`` declines the whole
    tick without touching any skip bucket, so before this field health telemetry
    could not tell "queue deep, capacity full" (healthy) from "cannot spawn"
    (broken). A deferral, never a failure."""

    spawn_budget_detail: dict = field(default_factory=dict)
    """The MEASURED numbers behind ``spawn_budget_blocked``, so the cap is a value
    a reader can check rather than a name to interpret: ``blocked_by`` (the gate),
    ``cap`` (the ceiling that was met), ``board_running`` (this board's running
    workers), ``host_cap``/``host_running`` (the host-wide pair, when that gate is
    the one that held), and ``deferred`` (how many cards it held). Card
    ``t_843cb0a9``: a saturated board read as a wedged one because the tick said
    nothing; the name alone still leaves "is the cap 8 and 8 running, or is the
    cap misread?" unanswerable from the output."""

    deferred_in_progress_capped: list[str] = field(default_factory=list)
    """Task ids the HOST-WIDE ``kanban.max_in_progress`` ceiling held back this
    tick (it was already met by the sum over every board). Same class as
    ``deferred_board_capped`` — no per-card refusal exists, so without this the
    host cap is the one budget gate that names nothing (card ``t_843cb0a9``,
    where a host at 12/8 reported every skip bucket empty). Counted by
    ``describe_suppression`` as ``in_progress_deferred=N``."""

    deferred_board_capped: list[str] = field(default_factory=list)
    """Task ids the BOARD'S OWN ceiling held back this tick: the board was
    already at ``kanban.max_spawn_by_board`` (or ``kanban.max_spawn``), so a free
    host slot could not start them. No per-card refusal exists for these — the
    tick simply looks idle from the outside — so they are named here and counted
    by ``describe_suppression`` (``board_cap_deferred=N``). Never a failure:
    nothing is killed to get under a ceiling, and the board still reclaims and
    promotes."""

    deferred_host_capped: list[str] = field(default_factory=list)
    """Task ids this board's SHARE of the free host slots could not cover, when
    more boards had startable work than the host budget had free slots. That is
    a rotation, not a fault, and it is recorded the same way so a board that
    keeps losing the race is still visible (``host_cap_deferred=N``) instead of
    reading as an idle queue."""

    spawn_failed: list[tuple[str, str, str]] = field(default_factory=list)
    """``(task_id, assignee, reason)`` for cards that were claimed and whose
    worker could NOT be launched this tick (workspace resolution or the spawn
    call raised); a ``spawn_failed`` task event is written alongside each. This
    is the operator-actionable "cannot spawn" signal — deferrals
    (``respawn_guarded``, ``skipped_per_profile_capped``, ``skipped_locked``,
    ``memory_pressure``, ``spawn_budget_blocked``) are not failures."""


# ---------------------------------------------------------------------------
# Dispatcher health telemetry (shared by the gateway watcher and the daemon)
#
# What this replaces: "ready work exists and 0 workers spawned for N ticks" —
# which is the steady state on a healthy but LOADED host, so the warning fires
# forever while nothing is wrong. `_tick_spawn_budget` declines an entire tick
# without recording any skip bucket once `kanban.max_spawn` /
# `kanban.max_in_progress` are consumed, so a deep queue and a wedged dispatcher
# looked identical; meanwhile a card that was claimed and could not be launched
# at all (the one signal an operator must act on) raised no alarm. The rules
# below tell the three states apart: deferred (silent), failed (warn at once,
# naming board/profile/task/reason), and stalled (nothing attempted, nothing
# explains it, old work waiting — warn after HEALTH_WINDOW ticks).
# ---------------------------------------------------------------------------
HEALTH_WINDOW = 6
"""Consecutive unexplained no-op ticks before the stall warning."""

HEALTH_STALL_AGE_SECONDS = 1800.0
"""Pending work younger than this is not stalled — it just arrived."""

HEALTH_WARN_INITIAL_INTERVAL = 300.0
"""Seconds before a repeat of an unchanged warning state (doubles per repeat)."""

HEALTH_WARN_MAX_INTERVAL = 3600.0
"""Ceiling for the repeat interval."""


@dataclass(frozen=True)
class PendingWork:
    """Longest-waiting spawnable card on a board, as a health probe sees it."""

    board: Optional[str] = None
    task_id: Optional[str] = None
    age_seconds: float = 0.0

    @property
    def pending(self) -> bool:
        """Some board has ready/review work the dispatcher could spawn for."""
        return self.task_id is not None


@dataclass
class HealthReport:
    """One health judgement worth emitting: ``level`` is ``"warning"``/``"info"``."""

    level: str
    message: str


# Fields whose non-empty value means the tick CHANGED something. A tick that did
# any work is not evidence of a wedged dispatcher, whatever it left queued.
_TICK_WORK_FIELDS = (
    "spawned",
    "reclaimed",
    "promoted",
    "crashed",
    "timed_out",
    "stale",
    "auto_blocked",
    "reconciled_orphans",
    "rate_limited",
    "auto_assigned_default",
)


def tick_did_work(result: Optional[object]) -> bool:
    """Did this tick change any state (spawn, reclaim, promote, crash, ...)?"""
    if result is None:
        return False
    return any(getattr(result, name, None) for name in _TICK_WORK_FIELDS)


def tick_deferral_reason(result: Optional[object]) -> Optional[str]:
    """Why this tick attempted no spawn ON PURPOSE, or ``None`` when it cannot say.

    Every reason comes from a bucket the dispatcher itself recorded, so a tick
    with a reason is loaded/guarded host behaviour doing the right thing — never a
    stall. ``None`` means "no spawn and nothing explains it", the only state worth
    watching.

    The whole-tick REFUSALS are deferrals too, and they have to be named here or
    moving to this tracker re-breaks the rulings that added them: a tick that
    yielded on the content-skew gate (``tick_yielded``, ruling ``t_8fed34c8``) and
    a board refused as not dispatch-enabled (``skipped_board_disabled``, card
    ``t_17c9c847``) are deliberate, loud, self-recorded decisions. Counted as
    unexplained no-ops, six of either with old work waiting accuse the healthy
    gate itself. ``deferred_host_capped`` is the same class — the board drew no
    share of this tick's free host slots, which its own field documents as "a
    rotation, not a fault".
    """
    if result is None:
        return None
    if getattr(result, "tick_yielded", ""):
        return "tick_yielded"
    if getattr(result, "skipped_board_disabled", False):
        return "board_disabled"
    if getattr(result, "deferred_host_capped", None):
        return "host_cap_deferred"
    budget = getattr(result, "spawn_budget_blocked", None)
    if budget:
        return f"spawn_budget:{budget}"
    pressure = getattr(result, "memory_pressure", None)
    if pressure:
        return f"memory_pressure:{pressure}"
    if getattr(result, "skipped_locked", False):
        return "board_held"
    if getattr(result, "respawn_guarded", None):
        return "respawn_guarded"
    if getattr(result, "skipped_per_profile_capped", None):
        return "per_profile_capped"
    if getattr(result, "skipped_unassigned", None):
        return "unassigned"
    return None


def collect_spawn_failures(
    board_results: Optional[Iterable[tuple[str, Optional[object]]]],
) -> list[tuple[str, str, str, str]]:
    """``(board, task_id, assignee, reason)`` for every failed launch this tick."""
    found: list[tuple[str, str, str, str]] = []
    for slug, result in board_results or []:
        for entry in getattr(result, "spawn_failed", None) or []:
            task_id, assignee, reason = (list(entry) + ["", "", ""])[:3]
            found.append(
                (slug or "?", str(task_id), str(assignee or "unassigned"), str(reason))
            )
    return found


def _boards_label(boards: list[str]) -> str:
    if not boards:
        return "an unnamed board"
    if len(boards) <= 2:
        return "board " + ", ".join(boards)
    return f"{len(boards)} boards ({', '.join(boards[:2])}, …)"


def spawn_failure_message(failures: list[tuple[str, str, str, str]], total: int) -> str:
    """Operator-facing line for launches that failed (claimed, never started)."""
    boards = sorted({b for b, *_ in failures})
    hint_board = boards[0] if boards else "default"
    detail = "; ".join(f"{tid} ({who}): {why}" for _b, tid, who, why in failures[:3])
    extra = "" if len(failures) <= 3 else f" (+{len(failures) - 3} more)"
    return (
        f"kanban dispatcher: {len(failures)} worker spawn failure(s) on "
        f"{_boards_label(boards)} this tick ({total} since the dispatcher was last "
        f"healthy) — {detail}{extra}. A spawn failure means the worker could not be "
        f"launched at all: check that profile's venv/PATH/credentials, "
        f"`hermes kanban list --board {hint_board} --status blocked` and errors.log."
    )


def stall_message(ticks: int, oldest: PendingWork) -> str:
    """Operator-facing line for a dispatcher that stopped attempting spawns."""
    minutes = int(oldest.age_seconds // 60)
    board = oldest.board or "?"
    return (
        f"kanban dispatcher[{board}]: {ticks} consecutive ticks spawned nothing and did "
        f"no other work while {oldest.task_id} waited ~{minutes} min for a worker — "
        f"spawnable, assigned, unclaimed, and no concurrency cap, guard or pause explains "
        f"it. The dispatcher loop itself is the suspect: check the gateway log for repeated "
        f"exceptions and `hermes kanban list --board {board} --status ready`."
    )


class DispatcherHealth:
    """Backlog-aware health tracker for one dispatcher loop.

    States: ``failed`` (a claimed card could not be launched — warn at once),
    ``stalled`` (no spawn attempted, nothing deferred it, and work older than
    :data:`HEALTH_STALL_AGE_SECONDS` is waiting — warn after
    :data:`HEALTH_WINDOW` ticks), otherwise healthy and silent. Capacity
    deferrals, guards, pauses and work that merely arrived are NOT trouble: on a
    loaded host they are the steady state.

    Repeats of an unchanged state wait an interval that doubles from
    :data:`HEALTH_WARN_INITIAL_INTERVAL` to :data:`HEALTH_WARN_MAX_INTERVAL`, so
    a long outage costs a handful of lines instead of one per tick, and returning
    to health emits exactly one ``info`` line.
    """

    def __init__(
        self,
        *,
        window: int = HEALTH_WINDOW,
        stall_age_seconds: float = HEALTH_STALL_AGE_SECONDS,
        warn_initial_interval: float = HEALTH_WARN_INITIAL_INTERVAL,
        warn_max_interval: float = HEALTH_WARN_MAX_INTERVAL,
    ) -> None:
        self.window = max(1, int(window))
        self.stall_age_seconds = float(stall_age_seconds)
        self.warn_initial_interval = float(warn_initial_interval)
        self.warn_max_interval = float(warn_max_interval)
        self.reset()

    def reset(self) -> None:
        """Forget all state — also used when dispatch is paused or slowed down."""
        self.stalled_ticks = 0
        self.failures_seen = 0
        self.state = "healthy"
        self._interval = self.warn_initial_interval
        self._last_report_at: Optional[float] = None
        self._reported = False

    def pause(self) -> None:
        """Dispatch is disallowed (``hermes pause``): an idle dispatcher, not a stall."""
        self.stalled_ticks = 0

    def observe_tick(
        self,
        board_results: Optional[Iterable[tuple[str, Optional[object]]]] = None,
        *,
        pending: Optional[PendingWork] = None,
        now: Optional[float] = None,
    ) -> Optional[HealthReport]:
        """Judge one tick; returns the line to emit, or ``None`` to stay silent."""
        at = time.time() if now is None else float(now)
        failures = collect_spawn_failures(board_results)
        if failures:
            self.failures_seen += len(failures)
            self.stalled_ticks = 0
            return self._emit(
                "warning", spawn_failure_message(failures, self.failures_seen), "failed", at
            )
        stalled = self._stall_message(board_results, pending or PendingWork())
        if stalled is not None:
            return self._emit("warning", stalled, "stalled", at)
        return self._clear()

    # -- internals --------------------------------------------------------
    def _stall_message(
        self,
        board_results: Optional[Iterable[tuple[str, Optional[object]]]],
        pending: PendingWork,
    ) -> Optional[str]:
        results = [res for _slug, res in board_results or []]
        # A tick is only suspicious when it attempted nothing AND nothing
        # explains why: any spawn, reclaim, promotion or crash is work, and any
        # recorded deferral (budget, memory, guard, board lock, unassigned) is a
        # decision. A deep queue under concurrency caps hits one of those every
        # tick, which is exactly the false positive this replaced.
        explained = any(tick_did_work(res) or tick_deferral_reason(res) for res in results)
        if not results or explained:
            self.stalled_ticks = 0
            return None
        if not pending.pending or pending.age_seconds < self.stall_age_seconds:
            self.stalled_ticks = 0
            return None
        self.stalled_ticks += 1
        if self.stalled_ticks < self.window:
            return None
        return stall_message(self.stalled_ticks, pending)

    def _emit(self, level: str, message: str, state: str, at: float) -> Optional[HealthReport]:
        if state != self.state:
            self.state = state
            self._interval = self.warn_initial_interval
        elif self._last_report_at is not None and at - self._last_report_at < self._interval:
            return None
        else:
            self._interval = min(self._interval * 2.0, self.warn_max_interval)
        self._last_report_at = at
        self._reported = True
        return HealthReport(level, message)

    def _clear(self) -> Optional[HealthReport]:
        if self.state == "healthy":
            return None
        previous, ticks, failures = self.state, self.stalled_ticks, self.failures_seen
        self.state = "healthy"
        self.stalled_ticks = 0
        self.failures_seen = 0
        self._interval = self.warn_initial_interval
        self._last_report_at = None
        if not self._reported:
            return None
        self._reported = False
        return HealthReport(
            "info",
            f"kanban dispatcher: healthy again — clearing {previous} "
            f"({ticks} stalled tick(s), {failures} spawn failure(s) while unhealthy)",
        )



def describe_suppression(results: Iterable[Optional[DispatchResult]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. Feeds the "dispatcher stuck" warnings of the
    CLI daemon and the embedded gateway dispatcher, which otherwise report a
    bare zero-spawn count while ``hermes kanban tail`` is the only place the
    guard reason is written (#111910).
    """
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.storage_locked:
            # A card looping on a contended state store: this counter is the whole
            # visibility surface for the class, so it must never be silent.
            counts["storage_locked"] = counts.get("storage_locked", 0) + len(res.storage_locked)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.skipped_board_disabled:
            # Board-level refusal: the board is an estate/scratch board and the
            # tick touched nothing (card t_17c9c847). Without this the refusal is
            # invisible to the "dispatcher stuck" line.
            counts["board_not_dispatch_enabled"] = counts.get("board_not_dispatch_enabled", 0) + 1
        if res.skipped_top_level_only:
            # The top-level-only gate's RUN door held these (hermes_cli/
            # kanban_top_level_gate.py; ruling t_efa908e8): the card is PARKED,
            # never silently skipped, so the tick's own line must name the hold --
            # a silent hold is how a starved board reads as an idle one.
            counts["skipped_top_level_only"] = counts.get("skipped_top_level_only", 0) + len(res.skipped_top_level_only)
        if res.substrate_held:
            # DOOR 3 (card t_0ae49984, design t_72e9bf03): these cards were not claimed because
            # their own workspace root was denied. They are still ready, so without this counter
            # the tick would read exactly like an idle one.
            counts["substrate_held"] = counts.get("substrate_held", 0) + len(res.substrate_held)
        if res.deferred_host_capped:
            # The HOST budget held these back: no per-card refusal of their own,
            # so without this the tick reads like an idle queue.
            counts["host_cap_deferred"] = counts.get("host_cap_deferred", 0) + len(res.deferred_host_capped)
        if res.deferred_board_capped:
            # The board's OWN ceiling held these back: like the host cap, no
            # per-card refusal exists, so a capped board would otherwise read
            # exactly like an idle one.
            counts["board_cap_deferred"] = counts.get("board_cap_deferred", 0) + len(res.deferred_board_capped)
        in_progress_held = getattr(res, "deferred_in_progress_capped", None)
        if in_progress_held:
            # The HOST-WIDE max_in_progress ceiling held these back. Without it
            # the one gate that stops every board at once (card t_843cb0a9: 12
            # running against a cap of 8) contributed NOTHING to this line, so
            # the "held back" annotation was empty exactly when the host was
            # saturated -- the state an operator most needs named.
            counts["in_progress_deferred"] = counts.get("in_progress_deferred", 0) + len(in_progress_held)
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    return ", ".join(parts)


def suppression_note(
    board_results: Optional[Iterable[tuple[str, Optional[Any]]]] = None,
) -> str:
    """``" Last tick held back: <reasons>."`` for a warning line, or ``""``.

    The live line's dispatcher-health warnings carry this annotation so an
    operator reads WHY ready work was held back instead of a bare zero-spawn
    count (rulings ``t_17c9c847`` / #111910). The prose lives here, beside the
    messages it annotates, so both surfaces emit one vocabulary rather than
    re-authoring the sentence at each call site.
    """
    held = describe_suppression([res for _slug, res in board_results or []])
    return f" Last tick held back: {held}." if held else ""


def stuck_ready_message(stats: dict) -> str:
    """Operator-facing line for ready cards nothing has ever attempted.

    Distinct from the suppression summary (``describe_suppression``: what the
    last tick held back) and from the ``skipped_nonspawnable`` bucket, which
    health telemetry suppresses precisely because ``hermes kanban assign`` — not
    the dispatcher — is the fix.
    """
    count = int(stats.get("count") or 0)
    task_id = stats.get("oldest_task_id") or "?"
    assignee = stats.get("oldest_assignee") or "(unassigned)"
    minutes = int((stats.get("oldest_age_seconds") or 0) // 60)
    board = stats.get("board")
    where = f"kanban dispatcher[{board}]" if board else "kanban dispatcher"
    if stats.get("oldest_spawnable") is False:
        why = (
            f"the oldest card's assignee {assignee!r} is not a live profile, so every "
            f"tick buckets it skipped_nonspawnable and no worker will EVER launch for "
            f"it — reassign it to a real profile or recreate it unassigned."
        )
    else:
        why = (
            f"the oldest card's assignee {assignee!r} is spawnable and unclaimed, so "
            f"nothing running it means the lane is starved (caps, guards, memory "
            f"pressure) or that worker never polls; check "
            f"`hermes kanban list --status ready` and the gateway log."
        )
    return (
        f"{where}: {count} ready card(s) have never been attempted by any worker; "
        f"oldest is {task_id} ({assignee}) waiting ~{minutes} min. {why}"
    )


# Bounded registry of recently-reaped worker exits, filled by the reap loop in
# ``dispatch_once`` and read by ``detect_crashed_workers`` to classify a dead-pid
# task. Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``; raw status kept so
# both WIFEXITED/WEXITSTATUS and WIFSIGNALED can be consulted. Trimmed by age
# plus a total size cap. Process-local by nature (``waitpid`` only reaps our own
# children): a per-tick ``hermes kanban dispatch`` process finds it empty, so
# ``_classify_dead_worker_exit`` falls back to the exit trailer the worker
# leaves in its own log (``KANBAN_WORKER_EXIT_TRAILER``).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: dict[int, tuple[int, float]] = {}

# Windows has no ``waitpid(-1)``: a child's exit code is only recoverable
# through a live handle, so ``_default_spawn`` parks each worker's ``Popen``
# here (Windows only) and ``reap_worker_zombies`` polls it. Entry: ``pid -> Popen``.
_live_worker_procs: dict[int, subprocess.Popen] = {}


def _wait_status_from_returncode(returncode: int) -> int:
    """Encode a ``Popen.returncode`` in the wait-status layout the registry stores."""
    return (int(returncode) & 0xFF) << 8


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status; duplicate pids overwrite (latest wins)."""
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> tuple[str, Optional[int]]:
    """``(kind, code)`` for a reaped worker PID: ``clean_exit`` (rc 0 while
    still ``running`` = protocol violation), ``rate_limited``
    (``KANBAN_RATE_LIMIT_EXIT_CODE``, never counts as a failure),
    ``storage_locked`` (``KANBAN_STORAGE_LOCKED_EXIT_CODE``, a state-store
    lock-stop — infrastructure, never counts as a failure),
    ``nonzero_exit``, ``signaled`` (``code`` is the signal), ``unknown`` (pid
    not in the reap registry; ``code`` None)."""
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    # Bit-level POSIX wait-status decode instead of os.WIFEXITED/WEXITSTATUS/
    # WIFSIGNALED/WTERMSIG: those helpers do not exist on Windows, where the
    # registry is fed by reap_worker_zombies' Popen poll. Low 7 bits = signal
    # (0 = normal exit, 0x7F = stopped), bits 8-15 = exit code.
    raw = int(raw)
    signal_number = raw & 0x7F
    if signal_number == 0:
        return _exit_code_kind((raw >> 8) & 0xFF)
    if signal_number != 0x7F:
        return ("signaled", signal_number)
    return ("unknown", None)


def _exit_code_kind(code: int) -> tuple[str, int]:
    """``(kind, code)`` for a worker's exit code, however it was observed."""
    if code == 0:
        return ("clean_exit", 0)
    if code == _kb.KANBAN_RATE_LIMIT_EXIT_CODE:
        return ("rate_limited", code)
    if code == _kb.KANBAN_STORAGE_LOCKED_EXIT_CODE:
        return ("storage_locked", code)
    if code == _kb.KANBAN_TERMINAL_PROVIDER_EXIT_CODE:
        return ("terminal_provider", code)
    return ("nonzero_exit", code)


_EXIT_TRAILER_RE = re.compile(
    r"^" + re.escape(KANBAN_WORKER_EXIT_TRAILER) + r"(\d+)\s*$", re.MULTILINE,
)


def _worker_log_exit_code(task_id: str, board: Optional[str] = None) -> Optional[int]:
    """Exit code from the trailer the worker CLI wrote to its own log; None when absent.

    The durable twin of ``_recent_worker_exits``: written by the worker itself
    (``hermes_cli.quiet_single_query.exit_single_query``), so it is there whether
    or not the process running this sweep ever reaped the worker. Last trailer
    wins — the log is append-mode across re-runs.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return None
    matches = _EXIT_TRAILER_RE.findall(raw or "")
    return int(matches[-1]) if matches else None


def reap_worker_zombies() -> list[int]:
    """Reap exited workers without blocking; returns reaped PIDs. POSIX reaps
    every child via ``waitpid(-1)``; Windows polls the ``Popen`` handles
    parked by ``_default_spawn`` (the only way to learn a child's exit code
    there), so the rate-limit sentinel exit is classified on both hosts."""
    reaped: list[int] = []
    if _kb._IS_WINDOWS:
        for pid, proc in list(_live_worker_procs.items()):
            returncode = proc.poll()
            if returncode is None:
                continue
            _record_worker_exit(pid, _wait_status_from_returncode(returncode))
            _live_worker_procs.pop(pid, None)
            reaped.append(pid)
        return reaped
    try:
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            _record_worker_exit(pid, status)
            reaped.append(pid)
    except Exception:
        pass
    return reaped


# ---------------------------------------------------------------------------
# Liveness: one witness, three answers
# ---------------------------------------------------------------------------
# A liveness read has to be able to answer "I cannot tell" (#123811). Reading a FAILED probe as death
# is what let a single dispatch tick declare six live workers dead — each recovery then spawned a
# duplicate beside the worker it had just abandoned. Every verdict here is one of the three below, and
# only ``WORKER_DEAD`` may release a claim or authorize a signal. ``WORKER_UNKNOWN`` holds the claim.
WORKER_ALIVE = "alive"
WORKER_UNKNOWN = "unknown"
WORKER_DEAD = "dead"

# A recorded fingerprint's start is expressed relative to a boot reference; two starts compare only
# while that reference holds. macOS moves ``kern.boottime`` with the system clock, shifting every start
# taken before the move by exactly the move — measured here as 800 cs (#124570). This tolerance covers
# only the centisecond rounding of two readers sharing one reference; a move of real size is far
# larger, and a move is not a difference (see ``boot_reference_held``).
BOOT_REFERENCE_TOLERANCE_CS = 2


def _exists_probe(pid: int) -> bool:
    """Is the PID number occupied (zombies included)?

    Goes through the ``kanban_db`` facade when it has been replaced — plugins and tests patch
    ``kanban_db._pid_alive`` to drive liveness, and the pre-existing code honoured that by calling
    ``_kb._pid_alive``. Doing that unconditionally would recurse here, because by default the facade IS
    this module's ``_pid_alive``; so only an OVERRIDE is consulted, and otherwise the probe is made
    directly.
    """
    facade = getattr(_kb, "_pid_alive", None)
    if facade is not None and facade is not _pid_alive:
        return bool(facade(int(pid)))
    from gateway.status import _pid_exists
    return bool(_pid_exists(int(pid)))


def _pid_liveness(pid: Optional[int]) -> str:
    """``alive`` / ``dead`` / ``unknown`` for a bare PID (no identity component).

    ``dead`` is reserved for proof: the OS says the number is unoccupied (``ESRCH``/``OpenProcess``
    failure) or the process is a zombie. A secondary probe that FAILED is not proof — macOS ``ps``
    exits non-zero for PIDs it cannot see, under load as well as after exit — so it answers
    ``unknown`` and leaves the caller's own evidence (a matching fingerprint) in charge. The probe
    timing out or raising keeps the ``kill(0)`` answer, which is the pre-existing behaviour.
    """
    if not pid or pid <= 0:
        return WORKER_DEAD
    if not _exists_probe(int(pid)):
        return WORKER_DEAD
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return WORKER_DEAD
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', errors='replace',
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                # A failed probe proves nothing: the number IS occupied (``_pid_exists`` said so
                # above), and "could not read the state" is not "the process is gone".
                return WORKER_UNKNOWN
            if "Z" in (proc.stdout or "").strip():
                return WORKER_DEAD
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return WORKER_ALIVE


def _pid_alive(pid: Optional[int]) -> bool:
    """True only when ``pid`` is PROVEN to be a running process (verdict ``alive``).

    Callers asking "is this number still occupied?" must not use the negation: ``unknown`` reads as
    False here, so ``not _pid_alive(...)`` would turn an unreadable probe into proof of death.
    Compare the verdict instead — ``_pid_liveness(pid) == WORKER_DEAD``.
    """
    return _pid_liveness(pid) == WORKER_ALIVE


# ``worker_started_at`` value for a spawn whose fingerprint could not be captured. Distinct from the
# NULL legacy row (pre-fingerprint spawn): such a worker is held (its claim is never released beside
# the live PID) but NEVER signalled — missing process identity is refusal, not permission (#99558).
UNVERIFIED_WORKER_FINGERPRINT = "unverified"


def _process_fingerprint(pid: int) -> Optional[str]:
    """Restart-stable identity of a live process: ``"<boot witness>|<boot reference>|<start>"``.

    The witness names the boot itself (``gateway.status.host_boot_witness``); the start is centiseconds
    since boot and the reference is the value that start is expressed against
    (``gateway.status.get_process_uptime_fingerprint``). A reboot breaks the pair, and — unlike the
    absolute form — so does nothing else while the reference holds. The reference is recorded because
    it does NOT always hold: macOS moves ``kern.boottime`` with the system clock, which shifts every
    start taken before the move by exactly the move, and a comparator that cannot see the move reads
    that shift as a recycled PID (#124570). A platform whose start is already boot-relative records
    ``0``. ``None`` when a half is unreadable — including "the platform has no boot witness", whose only
    honest reading is inability to certify identity, never a difference.

    The definition lives in ``hermes_cli.code_skew_content`` (card t_4dbee32e): a persisted boot
    record carries this identity so a carrier in ANOTHER process can tell "the same frozen process"
    from "a recycled pid", and the liveness contract here must agree with it byte for byte. One
    definition, two readers.
    """
    from hermes_cli.code_skew_content import process_identity

    return process_identity(int(pid))


# ---------------------------------------------------------------------------
# Liveness contract (R1, card t_d98471e5) — a DEPLOYED-BYTES contract, not a heuristic
# ---------------------------------------------------------------------------
# On 2026-09-29 the live dispatch path silently regressed to pre-#123811/#124570 liveness bytes
# (every spawn from 01:02:10 to 12:36:53 recorded the 2-field ``witness=""|absolute_start`` form)
# and the old comparator answered DEAD for live workers: claims were released and cards re-queued
# beside their live workers (runs 5642/5643, 08:41:06). A guard written INSIDE the fixed code
# cannot stop a deployment that is RUNNING OLDER BYTES, so the primary fix is a contract the
# running module DECLARES, which the reclaim path checks before it acts and a reload probe reads.
#
# Version ``LIVENESS_CONTRACT_ID`` whenever the fingerprint format or the verdict semantics
# change, and keep it beside the code it names so the two cannot drift silently.
LIVENESS_CONTRACT_ID = "witness-v1"

# The minimum contract the reclaim path requires before it may release ANY claim. A tree whose
# declared id is missing, unparseable or below this HOLDS: its bytes are older than the semantics
# they would be acting on. `_reclaim_dead_workers` refuses every release it would otherwise make
# and records `reclaim_refused {reason: "stale_liveness_contract"}`.
REQUIRED_LIVENESS_CONTRACT_ID = "witness-v1"

_liveness_contract_announced = False


def _liveness_contract_rank(contract_id: Any) -> Optional[int]:
    """``N`` for a ``"witness-vN"`` contract id, else ``None``.

    ``None`` means "unreadable", never ``0``: a module that declares nothing at all must FAIL
    the required check, not tie with a real contract of rank 0.
    """
    if not isinstance(contract_id, str):
        return None
    prefix = "witness-v"
    if not contract_id.startswith(prefix):
        return None
    try:
        return int(contract_id[len(prefix):])
    except (TypeError, ValueError):
        return None


def _liveness_contract_ok() -> bool:
    """True when the RUNNING module's declared contract is at or above the required one.

    Read from the module global at call time (never a frozen local), so a partially-updated
    tree or a downgraded constant is caught by the very path that would act on it.
    """
    declared = _liveness_contract_rank(globals().get("LIVENESS_CONTRACT_ID"))
    required = _liveness_contract_rank(REQUIRED_LIVENESS_CONTRACT_ID)
    if declared is None or required is None:
        return False
    return declared >= required


def announce_liveness_contract() -> Optional[str]:
    """Say ONCE per process which liveness contract this dispatcher is running.

    Called from the dispatch tick; the status prints only on the first call in the process, so
    a running dispatcher names its contract exactly once per boot. A reload/update leg greps
    the gateway log for ``liveness contract 'witness-v1'`` and reports the reload FAILED when
    the line is absent — a live tree that does not report the contract is the 2026-09-29 class.
    """
    global _liveness_contract_announced
    if _liveness_contract_announced:
        return None
    _liveness_contract_announced = True
    declared = globals().get("LIVENESS_CONTRACT_ID")
    ok = _liveness_contract_ok()
    line = (
        f"kanban dispatch: liveness contract {declared!r} "
        f"(required {REQUIRED_LIVENESS_CONTRACT_ID!r}, {'ok' if ok else 'stale'})"
    )
    if ok:
        logger.info(line)
    else:
        logger.error(line + " — reclaims are REFUSED until the tree carries a current contract")
    return line


def _fingerprint_parts(fingerprint: Any) -> Optional[tuple[str, int, Optional[int]]]:
    """``(witness, uptime-relative start, boot reference)`` for a fingerprint, else ``None``.

    ``None`` covers every shape that cannot be compared at all — a bare start tick (rows written before
    the witness existed) and a ``"|<start>"`` pair whose witness component is EMPTY (macOS rows written
    while ``current_instantiation_epoch`` had no answer there).

    A ``None`` REFERENCE (the two-field shape rows carry from before #124570) is different: it compares
    by exact match but never certifies a mismatch, because the reference it was built against is not in
    it — a mismatch there carries the same information as a moved reference, so a caller must read
    "unknown", never "recycled".
    """
    if not isinstance(fingerprint, str) or "|" not in fingerprint:
        return None
    witness, _, rest = fingerprint.partition("|")
    if not witness:
        return None
    fields = rest.split("|")
    try:
        if len(fields) == 1:
            return witness, int(fields[0]), None
        if len(fields) == 2:
            return witness, int(fields[1]), int(fields[0])
    except (TypeError, ValueError):
        return None
    return None


def _witnesses_agree(recorded: str, current: str) -> Optional[bool]:
    """True/False when two boot witnesses are comparable, ``None`` when they are not.

    Opaque witnesses (a boot id, a boot session UUID) must be EQUAL: they only ever change on a reboot.
    The boot-epoch fallback is the one form a clock step can move, so it is compared within
    ``START_TIME_DRIFT_TOLERANCE`` — the same tolerance the start component gets — instead of exactly.
    """
    from gateway.status import START_TIME_DRIFT_TOLERANCE
    if recorded == current:
        return True
    prefix = "boottime:"
    recorded_is_time = recorded.startswith(prefix)
    current_is_time = current.startswith(prefix)
    if recorded_is_time and current_is_time:
        try:
            gap = abs(int(recorded[len(prefix):]) - int(current[len(prefix):]))
        except (TypeError, ValueError):
            return None
        return gap <= START_TIME_DRIFT_TOLERANCE
    if recorded_is_time != current_is_time:
        # Mixed forms: one probe failed to yield its session id, so the witness changed SHAPE, not the
        # boot. "Cannot tell", never "different boot".
        return None
    return False


def _worker_liveness(pid: Optional[int], started_at) -> str:
    """The liveness verdict for a recorded worker: ``alive`` / ``dead`` / ``unknown``.

    ``dead`` needs proof of death (the PID is gone or a zombie) or proof of non-identity (the recorded
    fingerprint and the live reading disagree while the boot reference they are both expressed against
    still holds). Everything else is ``unknown``, and ``unknown`` never releases a claim: a reclaim that
    cannot prove the worker is gone must hold the claim and retry, because releasing it spawns a
    duplicate beside the process it abandoned. Fingerprints in an older shape — no witness, or no
    recorded reference to compare against — are ``unknown`` on a mismatch for the same reason:
    indistinguishable from a reference adjustment. Legacy NULL rows (no fingerprint at all) and
    deliberately UNVERIFIED spawns keep the existence answer they always had.

    A mismatch is only evidence while the reference holds. Measured 2026-09-27: macOS moved
    ``kern.boottime`` by 8.00 s, which put two live workers exactly 800 cs out of their own recorded
    start times (tolerance is 200 cs), and the sweep reclaimed both while they were running —
    ``dead`` there was a false verdict on a single reading, which is the defect this refuses
    (#124570).
    """
    if not pid or int(pid) <= 0:
        return WORKER_DEAD
    pid_state = _pid_liveness(int(pid))
    if pid_state == WORKER_DEAD:
        return WORKER_DEAD
    if started_at is None or started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Unchanged semantics today: these rows never had a comparable identity, so the
        # answer is the bare existence probe's, unprovable reading included.
        return WORKER_ALIVE if pid_state == WORKER_ALIVE else WORKER_DEAD
    parts = _fingerprint_parts(started_at)
    if parts is None:
        return WORKER_UNKNOWN
    witness, recorded_start, recorded_reference = parts
    from gateway.status import (
        get_process_uptime_fingerprint,
        host_boot_witness,
        start_time_fingerprints_match,
    )
    current_witness = host_boot_witness()
    if not current_witness:
        return WORKER_UNKNOWN
    same_boot = _witnesses_agree(witness, current_witness)
    if same_boot is None:
        return WORKER_UNKNOWN
    if not same_boot:
        # A different boot: the PID and the boot-relative tick can both recur.
        return WORKER_DEAD
    current = get_process_uptime_fingerprint(int(pid))
    if current is None:
        return WORKER_UNKNOWN
    current_reference, current_start = current
    if recorded_start <= 0 or current_start <= 0:
        return WORKER_UNKNOWN
    if start_time_fingerprints_match(recorded_start, current_start):
        return WORKER_ALIVE
    if not boot_reference_held(recorded_reference, current_reference):
        # The reference both readings are expressed against MOVED between the recording and this read,
        # so the start below is not the same measurement: two live workers were declared dead this way
        # (see the docstring). A comparison across a moved reference proves neither identity nor its
        # absence — hold, and let the TTL/backstop paths bound the wait.
        return WORKER_UNKNOWN
    return WORKER_DEAD


def boot_reference_held(
    recorded_reference: Optional[int], current_reference: Optional[int]
) -> bool:
    """True when both start readings were built against the same boot reference and may be compared.

    ``None`` (a fingerprint written before the reference was recorded) is never comparable: a mismatch
    there carries the same information as a moved reference, so the caller must read "hold", never
    "recycled". A small tolerance absorbs the centisecond rounding of the two readers sharing one
    ``kern.boottime``.
    """
    if recorded_reference is None or current_reference is None:
        return False
    return abs(int(recorded_reference) - int(current_reference)) <= BOOT_REFERENCE_TOLERANCE_CS


def _worker_alive(pid: Optional[int], started_at) -> bool:
    """True when ``pid`` is live AND is still the worker we spawned (verdict ``alive``).

    ``started_at`` is the fingerprint recorded by ``_set_worker_pid``; after a reboot (or any PID
    recycle) an unrelated process can own the number, so bare existence is never enough to extend a
    claim or to signal. For CLAIM PROTECTION use :func:`_worker_not_dead` — this one answers False for
    ``unknown``, which must never be read as "release it".
    """
    return _worker_liveness(pid, started_at) == WORKER_ALIVE


def _worker_not_dead(pid: Optional[int], started_at) -> bool:
    """Claim protection: keep the claim unless the worker is PROVEN dead (``alive`` or ``unknown``).

    This is the predicate every reclaim guard uses. Holding a claim beside a worker that may still be
    running costs a delayed retry; releasing it spawns a second worker on the same card, which is the
    duplication loop the reclaim exists to prevent.
    """
    return _worker_liveness(pid, started_at) != WORKER_DEAD


def _pid_recycled(pid: Optional[int], started_at) -> bool:
    """True when a live ``pid`` is PROVEN not to be the process fingerprinted at spawn.

    Signalling it would hit a stranger, and the reclaim may proceed: the worker we cared about is
    gone. An unprovable identity is NOT recycled (``unknown`` answers False here) — callers must
    consult the verdict, not this boolean, before releasing a claim.
    """
    return _worker_liveness(pid, started_at) == WORKER_DEAD


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int, started_at: Optional[int] = None) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is PROVEN gone.

    An ``unknown`` read keeps polling instead of answering "exited": the caller uses True to conclude
    the worker is gone, and an unreadable probe is not that conclusion.
    """
    for _ in range(10):
        if _worker_liveness(pid, started_at) == WORKER_DEAD:
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
    started_at=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths. ``started_at`` is the spawn-time
    fingerprint: when the live process no longer matches it, the PID was recycled and nothing is
    signalled — the worker is gone, which is what the reclaim wanted (``terminated`` = True). An
    UNVERIFIED spawn (fingerprint capture failed) that is still live is never signalled either, but
    it is reported as surviving (``signal_refused``) so the reclaim holds the claim instead of
    spawning a duplicate beside it."""
    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info
    if not str(claim_lock).startswith(_kb._host_prefix()):
        return info
    info["host_local"] = True

    kill = _kill_fn(signal_fn)
    if kill is None:
        return info
    if started_at == UNVERIFIED_WORKER_FINGERPRINT:
        # Never signal by bare number: a dead PID is "gone" (reclaim proceeds), a live one is held.
        info["signal_refused"] = True
        info["terminated"] = _pid_liveness(pid) == WORKER_DEAD
        return info
    verdict = _worker_liveness(pid, started_at)
    if verdict == WORKER_UNKNOWN:
        # The recorded identity cannot be compared with the live process (see ``_worker_liveness``).
        # Signalling the bare number could hit whatever now owns it, and reporting ``terminated`` would
        # release the claim beside a worker that may still be running. Hold both; retry next tick.
        info["signal_refused"] = True
        info["liveness_unknown"] = True
        info["terminated"] = False
        return info

    if verdict == WORKER_DEAD and _pid_liveness(pid) == WORKER_ALIVE:
        # Occupied by a stranger (the PID was recycled): never signal it. Our worker is gone.
        info["terminated"] = True
        info["pid_recycled"] = True
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        # Already gone = successful termination. Leaving terminated=False would
        # make the reclaim guard misread a dead worker as alive and defer forever.
        info["terminated"] = True
        return info
    except OSError:
        return info

    if _poll_worker_exit(pid, started_at):
        info["terminated"] = True
        return info
    if _worker_liveness(pid, started_at) == WORKER_ALIVE:
        if not _sigkill(kill, pid):
            return info
        info["sigkill"] = True
    info["terminated"] = _worker_liveness(pid, started_at) == WORKER_DEAD
    return info


def reap_terminal_workers(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """End host-local workers that outlived their run (issue #111791) — a worker
    that called ``kanban_complete`` and then hung keeps its ``state.db`` sidecar
    fds open and no ``running``-only sweep can see it once ``tasks.worker_pid`` is
    cleared. Keys on the closed ``task_runs`` row's retained pid + spawn
    fingerprint: a legacy row (NULL fingerprint) or a recycled PID is never
    signalled; a pid that is simply gone just has its evidence cleared. A run
    that ended less than ``TERMINAL_WORKER_REAP_GRACE_SECONDS`` ago is left
    alone so a worker still finalising after its own transition is not killed.
    One row's failure (signal, /proc probe) is logged and skips only that row.
    Returns the task ids whose worker was terminated."""
    rows = conn.execute(
        "SELECT id, task_id, worker_pid, worker_started_at, claim_lock FROM task_runs "
        "WHERE ended_at IS NOT NULL AND ended_at <= ? "
        "AND worker_pid IS NOT NULL AND worker_started_at IS NOT NULL",
        (int(time.time()) - TERMINAL_WORKER_REAP_GRACE_SECONDS,),
    ).fetchall()
    host_prefix = _kb._host_prefix()
    reaped: list[str] = []
    for row in rows:
        try:
            _reap_terminal_worker_row(conn, row, host_prefix, signal_fn, reaped)
        except Exception:
            _kb._log.debug(
                "kanban dispatch: terminal worker reap failed for run %s (task %s)",
                row["id"], row["task_id"], exc_info=True,
            )
    return reaped


def _reap_terminal_worker_row(conn, row, host_prefix: str, signal_fn, reaped: list[str]) -> None:
    pid, fingerprint = int(row["worker_pid"]), row["worker_started_at"]
    if pid == os.getpid() or not str(row["claim_lock"] or "").startswith(host_prefix):
        return
    if fingerprint == UNVERIFIED_WORKER_FINGERPRINT and _kb._pid_alive(pid):
        return  # unproven identity: never signalled; its evidence is cleared once the pid is gone
    verdict = _worker_liveness(pid, fingerprint)
    if verdict == WORKER_UNKNOWN:
        # Cannot certify identity. Clearing this row's pid would destroy the only handle on a worker
        # that may still be running, so leave the evidence for the next tick instead.
        return
    alive = verdict == WORKER_ALIVE
    termination = None
    if alive:
        termination = _terminate_reclaimed_worker(
            pid, row["claim_lock"], signal_fn=signal_fn, started_at=fingerprint)
        if not termination["terminated"]:
            return  # still alive: try again next tick
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET worker_pid = NULL, worker_started_at = NULL "
            "WHERE id = ? AND worker_pid = ? AND worker_started_at = ?",
            (row["id"], pid, fingerprint),
        )
        if alive:
            _kb._append_event(
                conn, row["task_id"], "terminal_worker_reaped",
                {"pid": pid, "worker_started_at": fingerprint, **termination}, run_id=row["id"],
            )
    if alive:
        reaped.append(row["task_id"])


def _worker_survived_termination(termination: dict) -> bool:
    """True when we tried to kill our own host-local worker and it is still alive.

    Reclaiming then would release the claim and spawn a second worker while the
    first still runs — the duplication loop. Only host-local workers we actually
    signalled count; a non-local lock or no-op attempt (no ``os.kill``) must fall
    through to the normal release path since we cannot manage that worker anyway.
    """
    return bool(
        termination.get("host_local")
        and (termination.get("termination_attempted") or termination.get("signal_refused"))
        and not termination.get("terminated")
    )


def _defer_reclaim_for_live_worker(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    termination: dict,
    *,
    reason: str,
) -> None:
    """Hold a claim whose worker survived termination instead of releasing it.

    Extends ``claim_expires`` by ``RECLAIM_DEFER_GRACE_SECONDS`` so the task
    stays ``running`` (no duplicate spawn) and records ``reclaim_deferred``.
    The next tick retries the kill; not spawning a duplicate is what lets the
    throttled worker finally die.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
            (grace, task_id, claim_lock),
        )
        if cur.rowcount != 1:
            return
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
        payload = {"reason": reason, "claim_lock": claim_lock, "claim_expires_now": grace}
        payload.update(termination)
        _kb._append_event(conn, task_id, "reclaim_deferred", payload, run_id=run_id)


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Liveness signal orthogonal to the PID check: a worker whose forked child
    (train loop, crawl) is stuck can still have a live Python process.
    Returns False if the task is not running or its claim expired.
    """
    now = int(time.time())
    with _kb.write_txn(conn):
        sql = "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ? AND status = 'running'"
        params: tuple = (now, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params += (int(expected_run_id),)
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _kb._current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id))
        _kb._append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    SIGTERM, short grace, then SIGKILL. Emits ``timed_out`` and restores the
    task's source phase so the next tick re-spawns the same kind of worker —
    unless the circuit breaker already gave up, leaving it blocked. Host-local
    only (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a test hook.

    The population is gated on a POSITIVE cap. The column's ``0`` is the author's
    documented "no cap" opt-out (``hermes_cli/kanban.py::_parse_duration`` normalises
    the CLI's ``0`` to unset for exactly this reason, and the stamp in
    ``_stamp_default_max_runtime_seconds`` treats a stored ``0`` as the card's own
    value), so it is NOT a budget: with ``limit == 0`` the ``elapsed < limit`` guard
    below is false for ANY elapsed and a ``0``-cap card is reaped on the very next
    tick with ``limit_seconds 0``. Gating the SELECT on ``> 0`` repairs rows already
    stored as ``0`` rather than only future writes.
    """
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = _kb._host_prefix()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND t.max_runtime_seconds > 0 "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt: ``tasks.started_at`` records the FIRST start,
        # so retries must be measured from the active task_runs row.
        elapsed = now - int(row["active_started_at"])
        limit = int(row["max_runtime_seconds"])
        if elapsed < limit:
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        started_at = _kb._row_get(row, "worker_started_at")
        verdict = _worker_liveness(pid, started_at)
        if verdict == WORKER_UNKNOWN:
            # Cannot certify identity: the number may belong to a stranger (no signal) and the worker
            # may still be running (no release beside it). Hold the claim; the next tick re-reads.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but its process "
                             "identity cannot be read; held, not signalled", tid, pid)
            continue
        if started_at == UNVERIFIED_WORKER_FINGERPRINT and verdict != WORKER_DEAD:
            # Fingerprint capture failed at spawn: we cannot prove this live PID is our worker, so
            # it is neither signalled nor released beside (duplicate). It is reclaimed once it exits.
            _kb._log.warning("kanban: task %s worker pid %s exceeded max runtime but has no verified "
                             "identity; not signalled", tid, pid)
            continue
        # SIGTERM then SIGKILL after 5 s grace; workers wanting a cleaner
        # shutdown install their own SIGTERM handler. A recycled PID (fingerprint
        # mismatch) is never signalled: the worker is already gone.
        killed = False
        kill = _kill_fn(signal_fn)
        stranger = verdict == WORKER_DEAD and _pid_liveness(pid) == WORKER_ALIVE
        if kill is not None and not stranger:
            with contextlib.suppress(ProcessLookupError, OSError):
                kill(pid, signal.SIGTERM)
            # Short polling wait — no time.sleep on the write txn.
            _poll_worker_exit(pid, started_at)
            if _worker_liveness(pid, started_at) == WORKER_ALIVE:
                killed = _sigkill(kill, pid)

        error = f"elapsed {int(elapsed)}s > limit {limit}s"
        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, tid, pid, row["claim_lock"]),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": limit,
                    "sigkill": killed,
                    "retry_status": retry_status,
                }
                run_id = _kb._end_run(
                    conn, tid, outcome="timed_out", status="timed_out",
                    error=error, metadata=payload,
                )
                _kb._append_event(conn, tid, "timed_out", payload, run_id=run_id)
                timed_out.append(tid)
        # Outside the write_txn above because ``_record_task_failure`` opens its
        # own. If the breaker trips this flips the task to ``blocked`` and emits
        # ``gave_up`` on top of the ``timed_out`` already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=error,
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed, "retry_status": retry_status},
            )
    return timed_out


# A running task with no heartbeat for this long is inactive regardless of
# ``dispatch_stale_timeout_seconds`` (spec: ">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks with no heartbeat progress; returns their ids.

    Stale = running longer than ``stale_timeout_seconds`` (active run's
    ``started_at``, else ``tasks.started_at``) AND ``last_heartbeat_at`` NULL or
    older than ``_STALE_HEARTBEAT_GAP_SECONDS``. Task returns to its source
    phase, run closes ``outcome='stale'``, a live host-local worker is killed.
    ``0`` disables the check; ``signal_fn`` is a test hook. Deliberately NOT
    counted via ``_record_task_failure``: an absent heartbeat is not a worker
    failure, and counting it would let long-running tasks trip the breaker.
    """
    if stale_timeout_seconds <= 0:
        return []

    now = int(time.time())
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        if row["active_started_at"] is None:
            continue
        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            continue

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        termination = _kb._terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn, started_at=_kb._row_get(row, "worker_started_at"))

        # Never release a claim while our own worker is still alive: that would
        # spawn a duplicate beside it. Hold the claim and retry next tick.
        if _worker_survived_termination(termination):
            _defer_reclaim_for_live_worker(
                conn, tid, lock, now, termination,
                reason="heartbeat_stale_worker_alive",
            )
            continue

        with _kb.write_txn(conn):
            retry_status = _kb._retry_status_for_run(conn, tid)
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ?",
                (retry_status, tid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": _kb._opt_int(last_hb),
                "heartbeat_age_seconds": _kb._opt_int(hb_age),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
                "retry_status": retry_status,
            }
            payload.update(termination)

            run_id = _kb._end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _kb._append_event(conn, tid, "stale", payload, run_id=run_id)
            reclaimed.append(tid)

    return reclaimed


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    """Requeue ``running`` cards with broken claim bookkeeping; returns their ids.

    A task ``running`` with NULL ``claim_lock``/``claim_expires`` (crash
    mid-claim, manual SQL, DB restore) is a zombie forever: ``release_stale_claims``
    needs ``claim_expires``, ``detect_crashed_workers`` needs a host-local lock +
    pid, ``detect_stale_running`` is off by default. Orphans go back to ``ready``
    with a comment, leaked run closed, ``reconciled`` event; a row with a live
    host-local PID is deferred so no duplicate spawns beside it.
    """
    now = int(time.time())
    reconciled: list[str] = []
    rows = conn.execute(
        "SELECT id, claim_lock, claim_expires, worker_pid, worker_started_at FROM tasks "
        "WHERE status = 'running' "
        "  AND (claim_lock IS NULL OR claim_expires IS NULL)"
    ).fetchall()
    for row in rows:
        tid = row["id"]
        pid = row["worker_pid"]
        verdict = _worker_liveness(pid, _kb._row_get(row, "worker_started_at")) if pid else WORKER_DEAD
        if verdict != WORKER_DEAD:
            # Never requeue beside a live process — nor beside one whose identity cannot be read:
            # the reclaim would spawn a duplicate next to a worker that may still be running
            # (#123811). Retry next tick.
            _kb._log.debug(
                "kanban reconcile: task %s has broken claim bookkeeping but "
                "pid %s is %s on this host — deferring", tid, pid, verdict,
            )
            # Record the deferral. ``detect_crashed_workers`` refuses to pronounce a reclaimed run
            # a crash without a reason from DISOWNED_RUN_REASONS, and this event is what makes the
            # later recovery honest: without it the next sweep would read a live worker's card as
            # a dead one whose attempt must be written off.
            with _kb.write_txn(conn):
                _kb._append_event(
                    conn, tid, "reclaim_deferred",
                    {
                        "reason": "orphaned_running_worker_alive",
                        "liveness": verdict,
                        "claim_lock": row["claim_lock"],
                        "claim_expires": _kb._opt_int(row["claim_expires"]),
                        "worker_pid": int(pid) if pid else None,
                        "now": now,
                    },
                )
            continue
        with _kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND claim_lock IS ? AND claim_expires IS ?",
                (tid, row["claim_lock"], row["claim_expires"]),
            )
            if cur.rowcount != 1:
                continue
            payload = {
                "reason": "orphaned_running",
                "claim_lock": row["claim_lock"],
                "claim_expires": _kb._opt_int(row["claim_expires"]),
                "worker_pid": int(pid) if pid else None,
                "now": now,
            }
            run_id = _kb._end_run(
                conn, tid,
                outcome="reclaimed", status="reclaimed",
                error="orphaned running card (broken claim bookkeeping)",
                metadata=payload,
            )
            _kb._insert_comment(
                conn, tid, "dispatcher",
                "reconciliation: card was 'running' with no valid claim "
                "(dead/gone worker) — requeued to ready",
                now,
            )
            _kb._append_event(conn, tid, "reconciled", payload, run_id=run_id)
            # The reclaim has no worker evidence: nobody observed a crash, the card simply
            # lost its claim bookkeeping. Reconcile the run it closed with an empty completion so
            # that row is not the attempt history's last word, and so the failure counter the
            # recovery booked does not stand against a worker that never failed.
            _kb._synthesize_empty_completion_run(conn, tid, disowned_run_id=run_id)
            reconciled.append(tid)
        _kb._log.info(
            "kanban reconcile: requeued orphaned running task %s "
            "(claim_lock=%r, worker_pid=%r)", tid, row["claim_lock"], pid,
        )
    return reconciled


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message (strip PIDs, timestamps) so same-root-cause errors group."""
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


# ~96% of "clean exit without a terminal tool call" tasks complete on a later
# run, so a protocol violation gets a bounded retry before the breaker trips.
# The budget is a violation-only STREAK (``_protocol_violation_streak``),
# independent of ``consecutive_failures``: other failure kinds neither consume
# nor extend it. Per-task ``max_retries`` overrides it.
_PROTOCOL_VIOLATION_FAILURE_LIMIT = 3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` runs are neutral and skipped (a quota wall
    says nothing about the task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "rate_limited":
            continue
        if outcome == "crashed" and (
            _kb._json_dict(row["metadata"]).get("protocol_violation")
            or "protocol violation" in (row["error"] or "")
        ):
            streak += 1
            continue
        break
    return streak


_PROTOCOL_VIOLATION_ERROR = (
    # Worker subprocess returned 0 but its task is still ``running`` in the DB — it exited without calling
    # ``kanban_complete`` / ``kanban_block`` / ``kanban_request_review``. Overwhelmingly the work itself succeeded and only the
    # paperwork was skipped, so a retry usually completes; the corrective sentence below is surfaced to the
    # retry worker via the prior-attempt error in ``build_worker_context`` (guidance approach from #61817).
    # Keep this short: ``_record_task_failure`` caps the stored error at 500 chars and the worker's own
    # last output (``_worker_final_output``, up to 400 chars) is appended after it — a longer preamble
    # truncates away the worker's explanation, which is the part the board and the retry worker need.
    "worker exited cleanly (rc=0) without kanban_complete, kanban_block "
    "or kanban_request_review — protocol violation. "
    "If the prior run already did the work, verify it and "
    "report it via kanban_complete (or kanban_request_review); "
    "a run without a terminal kanban call counts as failed no "
    "matter what it did."
)


# Rich panel/rule chrome around the rendered response, and the CLI's own preamble lines.
_LOG_CHROME = re.compile(r"[─━═╭╮╰╯│┃┌┐└┘]+|☤\s*Hermes")


def _exit_summary_marker() -> str:
    """The CLI exit-summary header (``cli_session_mixin.show_exit_summary``), in the active language."""
    from agent.i18n import t
    return t("cli.session.exit_resume_hint")


def _log_noise_prefixes() -> tuple[str, ...]:
    from agent.i18n import t
    return ("session_id:", "Query:", t("cli.chat.initializing_agent"))


def _worker_final_output(task_id: str, board: Optional[str] = None) -> str:
    """Best-effort read of a dead worker's last printed text, for the board diagnostic.

    A ``chat -q`` worker's stdout/stderr are redirected to its per-task log
    (``_default_spawn``), so when it exits without a terminal board call the
    reason is usually sitting there: the model's own explanation of why it could
    not comply (#88603), or the rendered provider error (#46593). The reap used to
    discard it in favour of a canned message on every retry. Trims the CLI exit
    summary, rule lines and the ``session_id:`` trailer; returns "" (never raises)
    on a missing/empty log.

    ``board`` must come from the dispatching tick: ambient current-board resolution
    is wrong for every board but the one the dispatcher thread happens to call
    "current", so the log would silently not be found.
    """
    try:
        raw = _kb.read_worker_log(task_id, tail_bytes=4000, board=board)
    except Exception:
        return ""
    if not raw:
        return ""
    raw = _EXIT_TRAILER_RE.sub("", raw)
    cut = raw.rfind(_exit_summary_marker())
    if cut != -1:
        raw = raw[:cut]
    lines = []
    for ln in raw.splitlines():
        ln = _LOG_CHROME.sub("", ln).strip()
        if ln and not ln.startswith(_log_noise_prefixes()):
            lines.append(ln)
    return " ".join(lines)[-400:]


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    terminal_provider: bool = False
    """``KANBAN_TERMINAL_PROVIDER_EXIT_CODE``: the provider rejected the worker's
    credential/model — trips the breaker on this first occurrence."""
    storage_locked: bool = False
    """``KANBAN_STORAGE_LOCKED_EXIT_CODE``: the turn was stopped by the profile
    state store's write lock being held by a peer — infrastructure, so the run is
    booked ``storage_locked`` and NEVER counted against the breaker."""

    @property
    def run_outcome(self) -> str:
        # A rate-limited requeue is recorded as ``rate_limited`` so board history
        # doesn't show a phantom crash for a quota wall; a storage-locked requeue
        # likewise records ``storage_locked`` so neither a quota wall nor a busy
        # store reads as a crash of the card's own work.
        if self.storage_locked:
            return "storage_locked"
        return "rate_limited" if self.rate_limited else "crashed"


def _classify_dead_worker(
    pid: int, claimer: Optional[str], *, task_id: Optional[str] = None, board: Optional[str] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    A clean exit or a crash carries the worker's own last output (``worker_output``
    in the event payload, appended to the error text) so the board and the retry
    worker see WHY instead of a bare label; a rate-limited or storage-locked
    requeue does not need it. Excluding the storage-locked branch is load-bearing:
    that requeue's stamped error text must stay free of arbitrary worker prose,
    which ``check_respawn_guard`` re-scans against ``_RESPAWN_BLOCKER_RE``.
    """
    dead = _classify_dead_worker_exit(pid, claimer, task_id=task_id, board=board)
    if task_id and not (dead.rate_limited or dead.storage_locked):
        worker_output = _worker_final_output(task_id, board=board)
        if worker_output:
            dead.error_text += f" Worker's last output: {worker_output!r}"
            dead.event_payload["worker_output"] = worker_output
    return dead


def _classify_dead_worker_exit(
    pid: int,
    claimer: Optional[str],
    *,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
) -> _DeadWorker:
    """Exit status -> reclaim bookkeeping, before the worker's own words are folded in.

    The reap registry only knows children of THIS process; a per-tick dispatcher
    reads the exit trailer the worker left in its log instead, so the same death
    gets the same booking (protocol violation / rate-limit requeue / crash) as
    under the gateway-embedded dispatcher. A worker that never reached its exit
    epilogue (killed, OOM) leaves no trailer and stays a plain crash.
    """
    kind, code = _classify_worker_exit(pid)
    if kind == "unknown" and task_id:
        logged = _worker_log_exit_code(task_id, board=board)
        if logged is not None:
            kind, code = _exit_code_kind(logged)
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, _PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "storage_locked":
        # The profile state DB's single write lock was held by a peer past the
        # transcript patience (another worker of this lane, the gateway, the
        # dashboard, or a maintenance checkpoint/VACUUM) — infrastructure, not this
        # card. Release to the source phase and do NOT count a failure, so a busy
        # store can never park a card whose own work was fine. The text is the
        # ONLY thing stamped for this outcome (no worker output is appended, see
        # ``_classify_dead_worker``) and it deliberately avoids every
        # ``_RESPAWN_BLOCKER_RE`` word, so ``check_respawn_guard`` reads the RUN
        # OUTCOME instead of trapping the card on the stamped line.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited with code {code}: the turn stopped on a transient state-store lock; "
            "the card was re-queued without counting a failure",
            "storage_locked",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code},
            storage_locked=True,
        )
    if kind == "terminal_provider":
        # The worker classified its own provider failure as unhealable (credential
        # revoked, model gone): every further spawn would hit the same wall, so
        # ``_account_crashes`` trips the breaker now instead of after ``failure_limit``.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
            "this profile's credential or model — fix the configuration, then unblock.",
            "crashed",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code, "terminal_provider": True},
            terminal_provider=True,
        )
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)


@dataclass
class _CrashSweep:
    """Everything ``detect_crashed_workers`` collects inside its reclaim txn."""

    crashed: list[str] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    # Requeued WITHOUT counting a failure because the profile state store's write
    # lock was held by a peer (``KANBAN_STORAGE_LOCKED_EXIT_CODE``). Held apart
    # from ``crash_details`` so a storage lock-stop can never reach
    # ``_account_crashes`` and never trips the breaker.
    storage_locked: list[str] = field(default_factory=list)
    # ``(task_id, pid, claimer, dead_worker)``: accounted after the txn via
    # ``_record_task_failure`` (needs its own write_txn).
    crash_details: list[tuple[str, int, str, _DeadWorker]] = field(default_factory=list)
    # Worker-exit observer payloads, fired only after every reclaim/accounting
    # txn has committed.
    exited_hook_payloads: list[dict] = field(default_factory=list)
    # The guard's whole decision for this sweep (the R4 count verdict and the R6 held rows), or
    # ``None`` when the sweep was never assessed.
    assessment: Optional[_crash_guard.SweepAssessment] = None


# ---------------------------------------------------------------------------
# Heartbeat veto (R2, card t_d98471e5)
# ---------------------------------------------------------------------------
# A `running` row whose own `last_heartbeat_at` is inside this window is NOT reclaimed, whatever
# the pid verdict says: the worker is talking to us, so the verdict is wrong about the worker we
# care about. Default 0 = OFF so tests and other callers are unaffected; prod arms it at 2x the
# automatic-heartbeat cadence (the agent bridge beats at most once every
# `tools.kanban_tools._AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS` = 60 s, so 2x that is the smallest
# window that cannot miss a live worker between beats) via
# `HERMES_KANBAN_HEARTBEAT_VETO_SECONDS=120` in the gateway environment.
#
# Safety is MONOTONE: the veto can only convert a reclaim into a hold, and it can never delay a
# TRUE reclaim by more than the window. It closes the false-dead class for ANY wrong pid answer
# inside fixed code (invisible pid, recycled pid, unreadable probe).
#
# It would NOT have prevented the 2026-09-29 runs (5642/5643): those bytes predated this guard
# and never read a heartbeat at all. The contract (R1) is what covers THAT class.
HEARTBEAT_VETO_SECONDS = 0


def resolve_heartbeat_veto_seconds() -> int:
    """The veto window: ``HEARTBEAT_VETO_SECONDS``, armable in prod without a code change via
    ``HERMES_KANBAN_HEARTBEAT_VETO_SECONDS``. Unreadable/invalid values fall back to the
    constant; a negative value is clamped to 0 (off), never a negative window."""
    raw = os.environ.get("HERMES_KANBAN_HEARTBEAT_VETO_SECONDS")
    if raw:
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            pass
    try:
        return max(0, int(HEARTBEAT_VETO_SECONDS))
    except (TypeError, ValueError):
        return 0


def _hold_claim_in_txn(
    conn: sqlite3.Connection,
    task_id: str,
    claim_lock: Optional[str],
    now: int,
    *,
    event_kind: str,
    reason: str,
    extra: Optional[dict] = None,
) -> None:
    """Extend a running claim's expiry and record WHY it was not released.

    ASSUMES an open write txn (caller holds one). No release, no failure accounting, no
    re-spawn: the row stays ``running`` and is re-read next tick. Mirrors
    ``_defer_reclaim_for_live_worker``'s vocabulary (``reclaim_deferred``) and adds
    ``reclaim_refused`` for the stale-contract case; neither counts a failure.
    """
    grace = now + _kb.RECLAIM_DEFER_GRACE_SECONDS
    cur = conn.execute(
        "UPDATE tasks SET claim_expires = ? "
        "WHERE id = ? AND status = 'running' AND claim_lock IS ?",
        (grace, task_id, claim_lock),
    )
    if cur.rowcount != 1:
        return
    run_id = _kb._current_run_id(conn, task_id)
    if run_id is not None:
        conn.execute("UPDATE task_runs SET claim_expires = ? WHERE id = ?", (grace, run_id))
    payload = {
        "reason": reason,
        "claim_lock": claim_lock,
        "claim_expires_now": grace,
        "liveness_contract": globals().get("LIVENESS_CONTRACT_ID"),
    }
    if extra:
        payload.update(extra)
    _kb._append_event(conn, task_id, event_kind, payload, run_id=run_id)


def _reclaim_dead_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> _CrashSweep:
    """Release every host-local ``running`` task whose worker PID is dead AND may be released.

    The write is DECIDED before it is made (:mod:`hermes_cli.kanban_crash_sweep_guard`): every
    candidate is classified first, with NO writes (pass 1); rows whose recorded fingerprint was
    built by a different identity generation than this dispatcher's are HELD (R6/R2); and a sweep
    that would close ``>= kanban_crash_sweep_guard.mass_crash_threshold()`` unexplained-death runs
    in one tick is refused IN FULL (R4) -- no run ended, no claim released, nothing counted. The
    caller (:func:`detect_crashed_workers`) fires the loud path. A wrong refusal is a loud card; a
    silent board mutation is not.

    Two guards also stand between a dead-pid VERDICT and a release (card t_d98471e5). Both are
    EVALUATED in pass 1 and APPLIED in pass 2, so a refused tick writes nothing at all:

    * R1 liveness contract — if this module's declared ``LIVENESS_CONTRACT_ID`` is missing or
      below ``REQUIRED_LIVENESS_CONTRACT_ID``, NOTHING is released; each otherwise-reclaimable
      row is held with ``reclaim_refused {reason: "stale_liveness_contract"}``. A tree running
      bytes older than the witness-aware comparator is exactly the 2026-09-29 incident.
    * R2 heartbeat veto — a row whose ``last_heartbeat_at`` is inside the veto window
      (``resolve_heartbeat_veto_seconds()``; prod arms it, default 0 = off) MUST NOT be
      reclaimed on a pid verdict, and is held with
      ``reclaim_deferred {reason: "heartbeat_fresh"}``.

    Only PROOF of death releases a claim or spawns beside it (``_worker_not_dead``): a row whose
    verdict is ``alive`` or ``unknown`` is skipped entirely, because an unreadable start-time/probe
    read is not proof, and treating it as one put six duplicate workers on six live cards in a
    single tick (#123811). Held rows are re-read next tick, and the TTL/backstop paths bound the
    wait.
    """
    sweep = _CrashSweep()
    announce_liveness_contract()
    now = int(time.time())
    contract_ok = _liveness_contract_ok()
    veto_seconds = resolve_heartbeat_veto_seconds()
    write_shape = _crash_guard.identity_shape(_process_fingerprint(os.getpid()))
    with _kb.write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, worker_started_at, claim_lock, started_at, assignee, "
            "       last_heartbeat_at "
            "FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = _kb._host_prefix()
        # PASS 1 -- classify every candidate. NO writes: the guard's verdict is a decision about
        # the WHOLE sweep, so it cannot be reached one row at a time.
        pending: list[tuple[Any, int, _DeadWorker, str]] = []
        # R1/R2 holds, deferred so a refused tick writes nothing (see the docstring).
        withheld: list[tuple[str, str, str, Optional[str], dict]] = []
        for row in rows:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Launch-window grace so a freshly-spawned worker isn't reclaimed
            # before its PID is visible on /proc.
            started_at = _kb._row_get(row, "started_at")
            if started_at is not None and time.time() - started_at < _kb._resolve_crash_grace_seconds():
                continue
            if _worker_not_dead(row["worker_pid"], _kb._row_get(row, "worker_started_at")):
                # Alive, or unprovable. Only PROOF of death releases a claim or spawns beside it.
                continue

            pid = int(row["worker_pid"])
            # R1: the running module must declare a current contract before ANY release.
            if not contract_ok:
                withheld.append((
                    "reclaim_refused", "stale_liveness_contract", row["id"], row["claim_lock"],
                    {
                        "worker_pid": pid,
                        "declared_contract": globals().get("LIVENESS_CONTRACT_ID"),
                        "required_contract": REQUIRED_LIVENESS_CONTRACT_ID,
                    },
                ))
                continue
            # R2: the worker's own recent heartbeat vetoes the dead-pid verdict.
            last_hb = _kb._row_get(row, "last_heartbeat_at")
            hb_age = (now - int(last_hb)) if last_hb is not None else None
            if veto_seconds > 0 and hb_age is not None and hb_age < veto_seconds:
                withheld.append((
                    "reclaim_deferred", "heartbeat_fresh", row["id"], row["claim_lock"],
                    {
                        "worker_pid": pid,
                        "heartbeat_age_seconds": hb_age,
                        "veto_seconds": veto_seconds,
                    },
                ))
                continue

            dead = _classify_dead_worker(pid, row["claim_lock"], task_id=row["id"], board=board)
            retry_status = _kb._retry_status_for_run(conn, row["id"])
            pending.append((row, pid, dead, retry_status))

        # THE GUARD (R4 + R6). Reached before a single write: an incomparable fingerprint is HELD,
        # and a sweep that would close too many runs at one instant is refused in full.
        assessment = _crash_guard.assess_sweep(
            [
                _crash_guard.SweepClosure(
                    task_id=row["id"],
                    pid=pid,
                    kind=dead.kind,
                    shape=_crash_guard.identity_shape(_kb._row_get(row, "worker_started_at")),
                    protocol_violation=dead.protocol_violation,
                    terminal_provider=dead.terminal_provider,
                    rate_limited=dead.rate_limited,
                    claim_lock=row["claim_lock"],
                )
                for row, pid, dead, _ in pending
            ],
            write_shape=write_shape,
        )
        sweep.assessment = assessment
        if assessment.refusal_needed:
            # REFUSE the whole write. Nothing above wrote; nothing below runs -- not the R1/R2
            # holds, not one booking.
            return sweep
        for event_kind, reason, task_id, claim_lock, extra in withheld:
            _hold_claim_in_txn(
                conn, task_id, claim_lock, now,
                event_kind=event_kind, reason=reason, extra=extra,
            )
        held = frozenset(assessment.held)
        # PASS 2 -- book the survivors. A held row gets NO write: its claim stays, and the loud
        # path has already reported the shape divergence.
        for row, pid, dead, retry_status in pending:
            if row["id"] in held:
                continue
            dead.event_payload["retry_status"] = retry_status
            # The attempt is RECORDED, not discarded: an exit code alone says
            # nothing about how much of the run was spent, so the reaped attempt
            # also books its wall-clock duration and the runtime budget it was
            # working against. Same field names ``enforce_max_runtime`` uses, so
            # a run's cost reads the same whether it timed out or died.
            open_run = conn.execute(
                "SELECT started_at FROM task_runs "
                "WHERE task_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            run_started_at = _kb._row_get(open_run, "started_at") if open_run is not None else None
            if run_started_at:
                dead.event_payload["elapsed_seconds"] = max(0, now - int(run_started_at))
            budget = _kb._row_get(row, "max_runtime_seconds")
            if budget is not None:
                dead.event_payload["limit_seconds"] = int(budget)
            dead.event_payload.setdefault("exit_kind", dead.kind)
            dead.event_payload["liveness_contract"] = globals().get("LIVENESS_CONTRACT_ID")
            error_text = (
                f"{dead.error_text} "
                f"[liveness_contract={globals().get('LIVENESS_CONTRACT_ID')}]"
            )
            cur = conn.execute(
                "UPDATE tasks SET status = ?, claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL "
                "WHERE id = ? AND status = 'running' "
                "  AND worker_pid = ? AND claim_lock IS ?",
                (retry_status, row["id"], pid, row["claim_lock"]),
            )
            if cur.rowcount != 1:
                continue
            run_id = _kb._end_run(
                conn, row["id"],
                outcome=dead.run_outcome, status=dead.run_outcome,
                error=error_text,
                metadata=dict(dead.event_payload),
            )
            _kb._append_event(conn, row["id"], dead.event_kind, dead.event_payload, run_id=run_id)
            sweep.exited_hook_payloads.append({
                "task_id": row["id"],
                "assignee": row["assignee"],
                "run_id": run_id,
                "worker_pid": pid,
                "exit_kind": dead.kind,
                "exit_code": dead.code,
                "outcome": dead.run_outcome,
                "retry_status": retry_status,
            })
            if dead.rate_limited or dead.storage_locked or dead.protocol_violation:
                # Stamp last_failure_error WITHOUT touching ``consecutive_failures``:
                # a rate-limited requeue must show ``check_respawn_guard`` a quota
                # blocker; a storage-locked requeue shows it its own transient kind;
                # a below-budget protocol violation never reaches
                # ``_record_task_failure`` (which stamps this column), yet the
                # board UI and retry worker need the corrective message.
                conn.execute(
                    "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                    (error_text[:500], row["id"]),
                )
            if dead.rate_limited:
                sweep.rate_limited.append(row["id"])
            elif dead.storage_locked:
                # NEVER crash_details: a lock-stop is infrastructure, so it must not
                # reach ``_account_crashes`` (the breaker) and must not count toward
                # the mass-crash abort either.
                sweep.storage_locked.append(row["id"])
            else:
                sweep.crashed.append(row["id"])
                sweep.crash_details.append((row["id"], pid, row["claim_lock"], dead))
    return sweep


def _account_crashes(conn: sqlite3.Connection, crash_details: list) -> list[str]:
    """Count each crash against the breaker; returns the task ids it tripped.

    Protocol violations get a BOUNDED violation-only budget independent of
    ``consecutive_failures`` (per-task ``max_retries`` takes precedence);
    systemic same-error crashes (>= 3 identical fingerprints this tick) and
    terminal provider errors (credential revoked, model gone — a retry cannot
    heal them) trip immediately.
    """
    auto_blocked: list[str] = []
    fp_counts: dict[str, int] = {}
    for _, _, _, dead in crash_details:
        fp = _error_fingerprint(dead.error_text)
        fp_counts[fp] = fp_counts.get(fp, 0) + 1
    for tid, pid, claimer, dead in crash_details:
        error_text = dead.error_text
        if dead.protocol_violation:
            streak = _protocol_violation_streak(conn, tid)
            trow = conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (tid,)).fetchone()
            if trow is None:
                continue  # task deleted mid-loop
            task_override = _kb._row_get(trow, "max_retries")
            violation_limit = (
                int(task_override) if task_override is not None else _PROTOCOL_VIOLATION_FAILURE_LIMIT
            )
            if streak < violation_limit:
                # Below budget: already back at ``ready`` with the error stamped.
                # No ``_record_task_failure`` — must not consume the unified budget.
                continue
            # ``force_trip``: the decision (incl. per-task ``max_retries``) was
            # already made against the violation streak above.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=violation_limit,
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={
                    "pid": pid,
                    "claimer": claimer,
                    "protocol_violations": streak,
                    "protocol_violation_limit": violation_limit,
                },
            )
        elif dead.terminal_provider:
            # A retry cannot heal a revoked credential or a missing model, so
            # the whole ``failure_limit`` budget would be spent on identical
            # failures. ``force_trip`` blocks now, sticky: ``recompute_ready``
            # must not auto-resume it before the operator fixes the provider.
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                force_trip=True,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer, "terminal_provider": True},
            )
        else:
            is_systemic = fp_counts.get(_error_fingerprint(error_text), 0) >= 3
            extra = {"pid": pid, "claimer": claimer}
            if is_systemic:
                # Trips at 1, below any ``failure_limit``: hold it for an operator.
                extra["sticky"] = True
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if is_systemic else None,
                release_claim=False,
                end_run=False,
                event_payload_extra=extra,
            )
        if tripped:
            auto_blocked.append(tid)
    return auto_blocked


def detect_crashed_workers(conn: sqlite3.Connection, board: Optional[str] = None) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Restores the source phase immediately (no waiting for the claim TTL), for
    tasks claimed by *this host* only — other hosts' PIDs are meaningless.
    Clean exit while ``running`` is a protocol violation with a bounded
    violation-only retry budget; ``KANBAN_RATE_LIMIT_EXIT_CODE`` is a quota
    wall and ``KANBAN_STORAGE_LOCKED_EXIT_CODE`` is a state-store lock-stop —
    both are infrastructure, released WITHOUT counting a failure and surfaced
    via the ``_last_rate_limited`` / ``_last_storage_locked`` attributes (the
    return stays crashed-only).
    """
    sweep = _reclaim_dead_workers(conn, board=board)
    if sweep.assessment is not None and sweep.assessment.alerting:
        # LOUD, never silent: the refusal (R4) and the held rows (R6) both go to the log, to the
        # board as per-card evidence, and to the design authority as one idempotent acting card.
        _crash_guard.alert_sweep_guard(
            conn, sweep.assessment, board=board, tree=_crash_guard.dispatching_tree(),
        )
    detect_crashed_workers._last_crash_sweep_abort = (  # type: ignore[attr-defined]
        sweep.assessment.verdict if sweep.assessment is not None and sweep.assessment.refusal_needed
        else None
    )
    detect_crashed_workers._last_crash_sweep_held = (  # type: ignore[attr-defined]
        list(sweep.assessment.held) if sweep.assessment is not None else []
    )
    if sweep.assessment is not None and sweep.assessment.refusal_needed:
        # The sweep refused itself: NOTHING was closed, so the returned list is empty and the
        # crash accounting below must not run.
        detect_crashed_workers._last_auto_blocked = []  # type: ignore[attr-defined]
        detect_crashed_workers._last_rate_limited = []  # type: ignore[attr-defined]
        detect_crashed_workers._last_storage_locked = []  # type: ignore[attr-defined]
        return []
    # Outside the main txn: account each crash and maybe trip the breaker.
    auto_blocked = _account_crashes(conn, sweep.crash_details) if sweep.crash_details else []
    # Side-channel attributes keep the public ``list[str]`` return stable;
    # ``dispatch_once`` reads them to populate ``DispatchResult``. Rate-limited
    # and storage-locked requeues did NOT count a failure and are NOT crashes.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    detect_crashed_workers._last_rate_limited = sweep.rate_limited  # type: ignore[attr-defined]
    detect_crashed_workers._last_storage_locked = sweep.storage_locked  # type: ignore[attr-defined]
    # Fired only now, after the reclaim txn AND breaker accounting have
    # committed, so subscribers always observe fully durable board state.
    if sweep.exited_hook_payloads and _kb._kanban_observer_consumed("on_kanban_worker_exited"):
        _board = _kb.get_current_board()
        for hook_fields in sweep.exited_hook_payloads:
            hook_fields = dict(hook_fields)
            _kb._fire_kanban_lifecycle_hook(
                # Kanban worker-lifecycle, task-mutation, and dispatcher-tick observers (RFC #58548,
                # accepted as the design basis in the #64231 batch disposition; on_kanban_dispatch_tick is
                # the re-port of PR #56066). All five are observers only: return values are ignored, and
                # every fire site is fully best-effort, so a broken callback can never break dispatch or a
                # task mutation. Cost rule: every call site short-circuits on has_hook(), so when nothing
                # subscribes no payload is built and the hot paths (each dispatcher tick, each task write)
                # pay one dict probe. WHICH PROCESS: worker spawn/exit/stale-claim and the dispatch tick
                # fire in the DISPATCHER process (gateway-embedded dispatcher or ``hermes kanban
                # dispatch``); on_kanban_task_updated fires in whichever process committed the mutation
                # (CLI, worker, or the gateway-embedded dashboard API). Common kwargs (task-scoped hooks):
                # task_id: str, profile_name: str, board: str | None, assignee: str | None, run_id: int |
                # None. on_kanban_worker_spawned fires after ``spawn_fn`` returns AND the worker PID (when
                # one was reported) is durably persisted, per the RFC timing contract; like
                # kanban_task_claimed it runs inside the board's dispatch lock, so callbacks must stay fast.
                # Adds: worker_pid: int | None, workspace_path: str. Privacy: workspace_path is a filesystem
                # path and may reveal project layout or usernames.
                "on_kanban_worker_exited",
                hook_fields.pop("task_id"),
                board=_board,
                **hook_fields,
            )
    return sweep.crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int | None = None,
    force_trip: bool = False,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
    infrastructure: bool = False,
    metadata_extra: Optional[dict] = None,
) -> bool:
    """Record a non-success outcome and maybe trip the circuit breaker; every
    non-success path funnels through here so ``consecutive_failures`` stays
    consistent. Returns True when the task was auto-blocked.

    ``release_claim=True, end_run=True``: spawn-failure path (task still
    running with an open run — restore source phase or ``blocked``, release
    claim, close run). Both False: timeout/crash path (caller already restored
    the phase and closed the run; only the counter moves, a trip flips to
    ``blocked`` + ``gave_up``). Threshold: per-task ``max_retries`` >
    ``failure_limit`` > ``DEFAULT_FAILURE_LIMIT``. ``force_trip`` trips
    unconditionally (caller applied its own bounded-retry policy).

    ``infrastructure=True``: the host refused the spawn (no restart-safe scope,
    #114720) — nothing about the card ran, so the run and event are recorded
    with ``infrastructure: true`` but ``consecutive_failures`` is left alone and
    the breaker never trips; the card stays retryable and
    :func:`check_respawn_guard` spaces the retries.

    ``metadata_extra``: structured facts that belong on the run AND the event
    alongside the failure — the spawn-seam substrate guard puts
    ``substrate_import`` / ``substrate_signature`` here so the lane actor can
    dedupe on the signature later without re-deriving it from prose. Applied on
    both branches (charged and not), because the guard reads the events of the
    not-charged one.
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    error = error[:500]
    with _kb.write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries, current_run_id "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        retry_status = (
            _kb._retry_status_for_run(conn, task_id, row["current_run_id"])
            if release_claim
            else ("review" if row["status"] == "review" else "ready")
        )
        failures = int(row["consecutive_failures"]) + (0 if infrastructure else 1)

        # Per-task override wins over caller-supplied and default thresholds.
        task_override = _kb._row_get(row, "max_retries")
        if task_override is not None:
            effective_limit, limit_source = int(task_override), "task"
        else:
            effective_limit, limit_source = int(failure_limit), "dispatcher"

        if infrastructure or not (force_trip or failures >= effective_limit):
            if release_claim:
                # Spawn path: restore the claimed source phase + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (retry_status, failures, error, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error, task_id),
                )
            # Timeout/crash path's caller already emitted its own event.
            if end_run:
                detail = {"failures": failures, "retry_status": retry_status}
                if infrastructure:
                    detail["infrastructure"] = True
                if metadata_extra:
                    detail.update(metadata_extra)
                run_id = _kb._end_run(
                    conn, task_id, outcome=outcome, status=outcome, error=error, metadata=detail,
                )
                _kb._append_event(conn, task_id, outcome, {"error": error, **detail}, run_id=run_id)
            return False

        # Spawn path (release_claim) is still running and also clears claim
        # state; the timeout/crash path already did.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', "
            + ("claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, worker_started_at = NULL, "
               if release_claim else "")
            + "consecutive_failures = ?, last_failure_error = ? "
            "WHERE id = ? AND status IN ('running', 'ready', 'review')",
            (failures, error, task_id),
        )
        payload = {
            "failures": failures,
            "effective_limit": effective_limit,
            "limit_source": limit_source,
            "error": error,
            "trigger_outcome": outcome,
            "retry_status": retry_status,
        }
        run_id = None
        if end_run:
            # Only the spawn path has an open run to close.
            run_meta = {
                "failures": failures,
                "trigger_outcome": outcome,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
                "retry_status": retry_status,
            }
            if metadata_extra:
                run_meta.update(metadata_extra)
            run_id = _kb._end_run(
                conn, task_id, outcome="gave_up", status="gave_up", error=error,
                metadata=run_meta,
            )
        if force_trip:
            # The caller applied its own bounded policy, so the counter cannot
            # judge this block: ``recompute_ready`` holds it for an operator.
            payload["sticky"] = True
        if event_payload_extra:
            payload.update(event_payload_extra)
        if metadata_extra:
            payload.update(metadata_extra)
        _kb._append_event(conn, task_id, "gave_up", payload, run_id=run_id)
        return True


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + its restart-stable fingerprint (``_process_fingerprint``), and
    emit a ``spawned`` event carrying them. The fingerprint is what lets every later liveness/kill
    decision tell OUR worker from a process that recycled the PID after a reboot. A failed capture is
    persisted as ``UNVERIFIED_WORKER_FINGERPRINT``, never NULL: NULL is the legacy pre-fingerprint row
    whose bare-PID kill authority a new spawn must not inherit."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                     (int(pid), started_at, task_id))
        run_id = _kb._current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, run_id))
        _kb._append_event(conn, task_id, "spawned", {"pid": int(pid), "started_at": started_at}, run_id=run_id)


def adopt_worker_pid(conn: sqlite3.Connection, task_id: str, run_id: int, pid: int) -> bool:
    """Worker-side half of ``_set_worker_pid``, run by the worker before its first model call.

    A dispatcher killed between spawning the worker and ``_set_worker_pid`` leaves the run with no
    pid: no liveness check can see the worker, so a TTL expiry reclaims the card and spawns a second
    worker beside it. The worker fills the missing pid itself (``worker_registered``). False when
    ``run_id`` is no longer the card's live run: the card was reclaimed before this worker got here,
    and it must exit without working it."""
    started_at = _process_fingerprint(int(pid)) or UNVERIFIED_WORKER_FINGERPRINT
    with _kb.write_txn(conn):
        row = conn.execute("SELECT status, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?",
                           (task_id,)).fetchone()
        if row is None or row["status"] != "running" or row["current_run_id"] != int(run_id):
            return False
        # Liveness checks are host-local: a pid from another host (or pid namespace) proves nothing here.
        if row["worker_pid"] is None and (row["claim_lock"] or "").startswith(_kb._host_prefix()):
            conn.execute("UPDATE tasks SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, task_id))
            conn.execute("UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                         (int(pid), started_at, int(run_id)))
            _kb._append_event(conn, task_id, "worker_registered", {"pid": int(pid), "started_at": started_at},
                              run_id=int(run_id))
    return True


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on success. NOT called on spawn success: a
    spawn proves the worker could start, not that the run will succeed, so
    timeouts and crashes must accumulate across spawn boundaries.
    """
    with _kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


def check_respawn_guard(
    conn: sqlite3.Connection, task_id: str, *, lane: str = "ready",
) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready/review row before any claim attempt. Priority order:
    ``"substrate_cooldown"`` (latest run is a ``spawn_failed`` on THIS tree's
    own import disagreement — nothing of the card ran; never counted, and it
    never falls through to ``blocker_auth`` because the naming line quotes
    module paths the quota/auth pattern would match),
    ``"infrastructure_cooldown"`` (latest run is a ``spawn_failed`` the host
    refused — no restart-safe scope — within the cooldown; never counted),
    ``"rate_limit_cooldown"`` (latest run ``rate_limited`` within the cooldown;
    checked BEFORE ``blocker_auth`` because the requeue stamps a quota-flavored
    ``last_failure_error`` that would otherwise park the task forever — that
    path never increments ``consecutive_failures``), ``"storage_locked_cooldown"``
    (latest run ``storage_locked`` within its own cooldown; same placement and
    the same reason — the stamped state-store line is infrastructure text, not a
    blocker, and the run never incremented ``consecutive_failures``),
    ``"blocker_auth"``
    (quota/auth pattern; the breaker still trips eventually), then for the
    ready lane only ``"recent_success"`` (completed run within the window, unless
    a re-queue event arrived after it — a deliberate re-run) and ``"active_pr"``
    (PR URL in a recent comment; re-spawning risks a duplicate PR — unless a
    handoff event followed the comment: the named profile must work on that
    PR). The review lane skips the last two: they are the *inputs* to a review
    handoff. Stale / dead claim locks are NOT a guard reason — the reclaim
    passes own those.
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    now = int(time.time())

    # 1. Rate-limit cooldown — see docstring for why this precedes blocker_auth.
    #    LATEST run only: a newer crash/completion supersedes the rate-limit run.
    #    An infrastructure spawn refusal (#114720) shares the cooldown: the host
    #    condition is not the card's, so it retries forever, spaced, and never
    #    reaches the breaker.
    rl_cooldown = _kb._resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest_run is not None and latest_run["outcome"] == "spawn_failed":
        latest_meta = _kb._json_dict(latest_run["metadata"])
        if latest_meta.get("substrate_import"):
            # The dispatching tree's own modules disagree (spawn-seam guard, t_a2f35c4d): nothing
            # of the card ran, so the card is spaced by its OWN reason and RETURNS here either
            # way. Returning matters: the naming line quotes module paths, so it can contain a
            # word the quota/auth pattern matches (``agent/auth.py``) and ``blocker_auth`` would
            # park the card forever on a failure that was never the card's. Retry (spaced) until
            # the tree is repaired; the lane actor is what escalates.
            if rl_cooldown <= 0:
                return None
            ended_at = latest_run["ended_at"]
            if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
                return "substrate_cooldown"
            return None
        if rl_cooldown > 0 and latest_meta.get("infrastructure"):
            ended_at = latest_run["ended_at"]
            if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
                return "infrastructure_cooldown"
    if latest_run is not None and latest_run["outcome"] == "rate_limited":
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, skipping blocker_auth so
            # the stamped rate-limit text doesn't re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — return early so blocker_auth doesn't catch the
        # stamped rate-limit text; this path intentionally retries forever
        # (spaced by the cooldown) until quota returns or a real run supersedes it.
        return None

    # 1b. Storage-lock cooldown — the same contract as the rate-limit branch above,
    #     on its own kind and its own cooldown so the two never share a notice. The
    #     stamped text is infrastructure prose, so ``blocker_auth`` must not see it:
    #     the card is spaced by this cooldown and never consumes its give-up budget.
    if latest_run is not None and latest_run["outcome"] == "storage_locked":
        sl_cooldown = _kb._resolve_storage_locked_cooldown_seconds()
        if sl_cooldown <= 0:
            # Default 0: respawn immediately (the reclaim pass already spaced it by
            # a tick and the dispatcher's own cadence is the interval), skipping
            # blocker_auth so the stamped storage-lock line cannot re-trap the card.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < sl_cooldown:
            return "storage_locked_cooldown"
        # Cooldown elapsed — return early, exactly as the rate-limit path does.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.  A plain
    # crash is different: its persisted error includes the worker's last
    # captured output, which is context rather than a diagnosis and may contain
    # benign commands such as ``claude auth status`` (#117097).
    err = _kb._lossy_text(row["last_failure_error"])
    latest_outcome = latest_run["outcome"] if latest_run is not None else None
    if err and latest_outcome != "crashed" and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # Review-lane spawns stop here: a recent completed run and a fresh PR URL
    # are the canonical *inputs* to a review handoff, not duplicate-work signals.
    if lane == "review":
        return None

    # 3. Completed run within guard window. Exception: an explicit re-queue
    #    AFTER that success (done→ready drag, re-promotion, unblock, reclaim) is
    #    a deliberate "run it again" — otherwise a manual done→ready would sit
    #    silently held until the window elapses.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    recent_completed = conn.execute(
        "SELECT ended_at FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ? "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id, cutoff),
    ).fetchone()
    if recent_completed:
        completed_at = int(recent_completed["ended_at"] or 0)
        requeued_after = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND created_at >= ? "
            "AND kind IN ('status', 'promoted', 'unblocked', 'reclaimed') "
            "LIMIT 1",
            (task_id, completed_at),
        ).fetchone()
        if not requeued_after:
            return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR.
    #    Exception: a handoff AFTER the newest PR comment (operator reassign,
    #    reviewer changes_requested, review reopen) names the profile that must
    #    now work on THAT PR — a closer or the implementer finishing it, not a
    #    duplicate implementation (#111910). A crash/reclaim is not a handoff,
    #    so the worker that opened the PR is still not re-spawned against it.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT body, created_at FROM task_comments "
        "WHERE task_id = ? AND created_at >= ? ORDER BY created_at DESC",
        (task_id, pr_cutoff),
    ).fetchall():
        body = _kb._lossy_text(c["body"])
        if not (body and _RESPAWN_GUARD_PR_URL_RE.search(body)):
            continue
        events = conn.execute(
            # Strictly after: a same-second tie stays guarded (fail closed).
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND created_at > ? "
            "AND kind IN ('assigned', 'changes_requested', 'review_reopened', 'reopened')",
            (task_id, int(c["created_at"] or 0)),
        ).fetchall()
        if any(_is_handoff_event(e["kind"], e["payload"]) for e in events):
            return None
        return "active_pr"

    return None


def _is_handoff_event(kind: str, payload: Optional[str]) -> bool:
    """Only an ``assigned`` event that moves the card to a DIFFERENT profile is
    a handoff. A no-op re-assign (dev→dev via CLI/dashboard/``reassign
    --reclaim``), an unassign, or the dispatcher's own
    ``kanban.default_assignee`` write would otherwise lift ``active_pr`` for
    the very implementer that opened the PR. Events without ``from`` (written
    before it was recorded) are not trusted as handoffs — fail closed."""
    if kind != "assigned":
        return True
    data = _kb._json_or(payload, {})
    if not isinstance(data, dict) or data.get("source") == "kanban.default_assignee":
        return False
    to = data.get("assignee")
    return bool(to) and "from" in data and data["from"] != to


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    """``hermes_cli.profiles.profile_exists``, or ``None`` when it cannot be
    imported (local import avoids a cycle; callers fall back to trusting the
    assignee).

    When ``kanban.dispatch_profiles`` is set (#110995) the returned predicate
    additionally requires the assignee to be listed, fail-closed — so a card
    assigned to ``default`` is only claimable by homes that opted into it.
    Foreign assignees land in the existing ``skipped_nonspawnable`` bucket.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name, profile_exists
    except Exception:
        return None
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return profile_exists

    def _gated(name: str) -> bool:
        try:
            canon = normalize_profile_name(name)
        except ValueError:
            return False
        return canon in allowlist and bool(profile_exists(name))

    return _gated


def _dispatch_profile_allowlist(normalize_profile_name) -> Optional[frozenset]:
    """Per-home claim allowlist ``kanban.dispatch_profiles`` (#110995).

    On a shared board (one ``kanban.db`` mounted across several Hermes homes),
    every home's ``profile_exists`` returns True for ``default`` — the root
    profile every home has — so a card assigned to ``default`` is claimable by
    every home's dispatcher. A home opts out of foreign claims by declaring
    which assignees it may claim::

        kanban:
          dispatch_profiles: ["sage", "researcher"]   # or "sage,researcher"

    Returns ``None`` only when the key is absent from the user config (upstream
    behavior: any existing profile is claimable). A present value is
    fail-closed: an empty list, ``null`` or a bare ``dispatch_profiles:`` claims
    nothing. The user layer is read without the ``DEFAULT_CONFIG`` merge (whose
    ``None`` placeholder would make the key look present in every home), and a
    config read that raises also claims nothing — a corrupt config on a shared
    board must never widen this home's claim scope silently (#113620).
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        kanban = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})
    except Exception as exc:
        _kb._log.warning(
            "kanban: could not read kanban.dispatch_profiles (%s: %s) — "
            "this home claims no cards until the config is readable",
            type(exc).__name__, exc,
        )
        return frozenset()
    if not isinstance(kanban, Mapping) or "dispatch_profiles" not in kanban:
        return None
    raw = kanban["dispatch_profiles"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        _kb._log.warning(
            "kanban: kanban.dispatch_profiles is present but empty — this home "
            "claims no cards; omit the key to allow any existing profile"
        )
        return frozenset()
    names = [str(n) for n in raw] if isinstance(raw, (list, tuple)) else str(raw).split(",")
    allowed = set()
    for n in names:
        try:
            allowed.add(normalize_profile_name(n))
        except ValueError:
            continue
    return frozenset(allowed)


def dispatch_profile_allowlist_summary() -> str:
    """Human-readable resolution of ``kanban.dispatch_profiles`` for this home.

    Surfaced by ``hermes kanban diagnostics`` so an operator on a shared board
    can see what a home believes it may claim (#113620): ``any`` (key absent),
    the sorted allowed names, or ``none (fail-closed: ...)``.
    """
    try:
        from hermes_cli.profiles import normalize_profile_name
    except Exception as exc:
        return f"none (fail-closed: profiles unavailable: {exc})"
    allowlist = _dispatch_profile_allowlist(normalize_profile_name)
    if allowlist is None:
        return "any"
    if allowlist:
        return ", ".join(sorted(allowlist))
    return ("none (fail-closed: kanban.dispatch_profiles is present but names no valid "
            "profile, or the config could not be read — omit the key to allow any)")


def _has_spawnable(conn: sqlite3.Connection, status: str) -> bool:
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = ? AND assignee IS NOT NULL AND claim_lock IS NULL",
        (status,),
    ).fetchall()
    if not rows:
        return False
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    return any(profile_exists(row["assignee"]) for row in rows)


def oldest_spawnable_pending(
    conn: sqlite3.Connection, *, include_review: bool = True
) -> tuple[Optional[str], float]:
    """``(task_id, age_seconds)`` of the longest-waiting spawnable card.

    Same filter as :func:`has_spawnable_ready` (assigned, unclaimed, and the
    assignee maps to a real profile — control-plane lanes excluded), plus WHICH
    card and how long it has waited, so health telemetry can tell "work just
    arrived" from "work has been waiting since before the stall".

    ``age_seconds`` counts from ``created_at``: the tasks table keeps no
    "ready since" stamp, so this is an UPPER bound on time-in-ready — a card
    created days ago and readied a minute ago looks old. That is enough to decide
    whether a silent dispatcher is worth reporting; it is not evidence that a
    card has been waiting that long. ``(None, 0.0)`` when nothing is spawnable.
    """
    statuses = ["ready"]
    if include_review and review_dispatch_enabled():
        statuses.append("review")
    try:
        rows = conn.execute(
            "SELECT id, assignee, created_at FROM tasks "
            f"WHERE status IN ({','.join('?' for _ in statuses)}) "
            "AND assignee IS NOT NULL AND claim_lock IS NULL "
            "ORDER BY created_at ASC",
            tuple(statuses),
        ).fetchall()
    except sqlite3.Error:
        return None, 0.0
    if not rows:
        return None, 0.0
    profile_exists = _profile_exists_fn()
    now = time.time()
    for row in rows:
        if profile_exists is not None and not profile_exists(row["assignee"]):
            continue
        created = row["created_at"]
        # No usable timestamp -> age 0, i.e. "cannot show this is old", never a
        # fabricated age that would trip the stall warning on its own.
        return row["id"], (0.0 if created is None else max(0.0, now - float(created)))
    return None, 0.0


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """True iff a ready+assigned+unclaimed task maps to a real Hermes profile.

    Lets health telemetry tell "stuck" (``0 spawned`` with spawnable work) from
    "correctly idle" (only control-plane lanes waiting on ``claim_task``). Falls
    back to "any assigned" when ``profile_exists`` is unimportable.
    """
    return _has_spawnable(conn, "ready")


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """:func:`has_spawnable_ready` for the review column."""
    return _has_spawnable(conn, "review")


def review_dispatch_enabled() -> bool:
    """Whether review tasks dispatch automatically. Default true (Hermes ships
    ``sdlc-review``); operators disable it for human-only review boards.
    """
    try:
        from hermes_cli.config import load_config
        return bool((load_config() or {}).get("kanban", {}).get("review_dispatch", True))
    except Exception:
        return True


# Memory-aware dispatch guard: an uncapped board once OOM'd a 1 GiB host. Two
# safeguards — a memory-DERIVED default cap when none is configured
# (``resolve_max_in_progress``) and a live memory-PRESSURE guard inside the
# tick (``_memory_pressure_level``) because a static cap can't see other
# tenants. Both fail open: non-Linux / read error → no cap / "unknown".

# Assumed per-worker footprint for the derived cap; deliberately conservative
# so the cap errs toward fewer workers on small VMs.
MEMORY_GUARD_MB_PER_WORKER = 512

# Derived default bounds: never below 2 (smallest VM must still progress),
# never above 8 (more fan-out must be explicit in config).
DERIVED_MAX_IN_PROGRESS_FLOOR = 2
DERIVED_MAX_IN_PROGRESS_CEILING = 8


def _system_memory_sample() -> dict:
    """Best-effort system memory snapshot (KiB values), ``{}`` when unknown.

    Local import keeps ``kanban_db`` importable without the gateway package.
    Module-level indirection is also the test seam — conftest patches this to
    ``{}`` so results don't depend on the CI runner's live memory.
    """
    try:
        from gateway.lifecycle_ledger import sample_memory
        return sample_memory() or {}
    except Exception:
        return {}


def derive_default_max_in_progress(sample: Optional[Mapping[str, Any]] = None) -> Optional[int]:
    """Memory-derived default for ``kanban.max_in_progress`` when unset:
    ``clamp(MemTotal / MEMORY_GUARD_MB_PER_WORKER, FLOOR, CEILING)``. Returns
    ``None`` (no cap) when total memory is unknown, so macOS/Windows dev
    machines are unaffected.
    """
    if sample is None:
        sample = _system_memory_sample()
    total_kib = sample.get("mem_total_kib")
    if isinstance(total_kib, bool) or not isinstance(total_kib, int) or total_kib <= 0:
        return None
    workers = (total_kib // 1024) // MEMORY_GUARD_MB_PER_WORKER
    return max(DERIVED_MAX_IN_PROGRESS_FLOOR, min(workers, DERIVED_MAX_IN_PROGRESS_CEILING))


def resolve_max_in_progress(configured: Optional[int]) -> Optional[int]:
    """Effective global concurrency cap: explicit config wins, else the
    memory-derived default. All config-parsing callers route through this so
    both paths agree.
    """
    if configured is not None:
        return configured
    return derive_default_max_in_progress()


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.
    """
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_in_progress")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def count_running_tasks(conn: sqlite3.Connection) -> int:
    """Number of tasks in ``status='running'``.

    Used by the multi-board sweep to count OTHER boards' workers against the
    host-level budget — the memory-derived cap bounds the machine, not the
    board. Fails open to 0 so a broken board doesn't brick dispatch on healthy ones.
    """
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )
    except Exception:
        return 0


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    """Total ``running`` tasks across every board EXCEPT ``board``.

    Caps bound the HOST, but each board's tick only sees its own DB; without
    this a derived cap of N gets multiplied by the number of active boards.
    Boards are matched by resolved DB path, so ``HERMES_KANBAN_DB`` (pins every
    board to one file) yields 0. Fails open per board.
    """
    try:
        current_path = str(_kb.kanban_db_path(board=board).expanduser().resolve())
    except Exception:
        current_path = None
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return 0
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if current_path is not None and resolved == current_path:
                continue
            if not path.exists():
                continue
            other = _kbc.connect(board=slug)
            try:
                total += count_running_tasks(other)
            finally:
                with contextlib.suppress(Exception):
                    other.close()
        except Exception:
            continue
    return total


def measure_host_budget(
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    ceilings: Optional[Mapping[str, int]] = None,
    boards: Optional[Iterable[str]] = None,
) -> dict:
    """How much spawn capacity the CONFIG grants the host, as measured numbers.

    ``max_spawn`` is per BOARD while ``max_in_progress`` is per HOST, so a fleet of
    N dispatch boards advertises ``N x max_spawn`` local slots of which only
    ``max_in_progress`` can ever hold a worker. That arithmetic used to live only
    in the reader's head: ``kanban.max_spawn: 8`` x a handful of boards read as
    "8 slots", the host sat at 12 running, and nothing said which number was the
    real ceiling (card ``t_843cb0a9``).

    ``ceilings`` is the already-resolved per-board map (``kanban.max_spawn_by_board``
    where a board named one, the global value otherwise — the same resolution the
    gateway's ``max_spawn_for_board`` applies); an absent map means every board
    takes ``max_spawn``. ``boards`` overrides the enumeration (the dispatcher passes
    the slugs it actually visits); by default the dispatch-enabled boards are
    enumerated, and an unreadable inventory reports ``boards=None`` with no claim
    either way — a measurement that cannot be taken is never reported as a number.

    ``unreachable_slots`` is ``ceiling_sum - max_in_progress`` floored at 0, and it
    is ``None`` (not 0) when no host cap is derived: uncapped dispatch wastes
    nothing, and 0 would read as "measured, nothing wrong".
    """
    caps = {str(k): int(v) for k, v in (ceilings or {}).items() if v is not None}
    slugs: Optional[list[str]]
    if boards is not None:
        slugs = [str(slug) for slug in boards]
    else:
        try:
            slugs = [
                (meta.get("slug") or _kb.DEFAULT_BOARD)
                for meta in _kb.list_dispatch_boards()
            ]
        except Exception:
            slugs = None

    per_board: dict[str, Optional[int]] = {}
    if slugs is not None:
        for slug in slugs:
            cap = caps.get(slug)
            per_board[slug] = int(cap) if cap is not None else (
                int(max_spawn) if max_spawn is not None else None
            )
    ceiling_sum: Optional[int] = None
    if slugs is not None:
        resolved = [per_board[slug] for slug in slugs]
        if resolved and all(cap is not None for cap in resolved):
            ceiling_sum = sum(int(cap) for cap in resolved if cap is not None)

    unreachable: Optional[int] = None
    oversubscribed = False
    if ceiling_sum is not None and max_in_progress is not None:
        unreachable = max(int(ceiling_sum) - int(max_in_progress), 0)
        oversubscribed = int(ceiling_sum) > int(max_in_progress)

    return {
        "max_spawn": int(max_spawn) if max_spawn is not None else None,
        "max_in_progress": int(max_in_progress) if max_in_progress is not None else None,
        "boards": slugs,
        "ceilings": per_board,
        "ceiling_sum": ceiling_sum,
        "unreachable_slots": unreachable,
        "oversubscribed": oversubscribed,
    }


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Consumer-side CONTENT skew gate on the tick (platform-stl ruling t_8fed34c8, R2 + R3)
# ---------------------------------------------------------------------------
#
# The contract: a checkout rewrite must never leave a long-lived consumer serving a mixed
# revision. ``sys.path``/``sys.modules`` are per-process, so the writer cannot reach a consumer
# that is already running -- the enforceable half lives in the CONSUMER, and the first (and worst)
# consumer is this dispatcher tick.
#
# Measured live 2026-10-10: gateway pid 56908 (booted 13:06:29) froze a patched ``hermes_cli/*``
# beside an unpatched ``agent/*``; the disk was repaired, the PROCESS kept serving, and every
# spawn died ``cannot import name 'ADVISORY_SKILLS_ENV' from 'agent.skill_commands'`` for ~2h56m
# after the repair. ``gateway.code_skew.detect_code_skew()`` returned ``None`` for that whole
# window: it fingerprints only ``.git/HEAD`` -> ref -> sha and never the working tree, and the
# incident moved the WORKING TREE with HEAD unchanged (an interrupted merge leaves HEAD at the
# pre-merge commit; the recovery restores paths "to HEAD"). So the decision here is CONTENT
# (``hermes_cli.tree_fingerprint``); the ref check rides along as a cheap fast path and can only
# ADD detection, never be the whole decision.
#
# What a refusal does, deterministically and with no model consulted: spawn nothing this tick,
# write a NON-GREEN status record a human reads, and file EXACTLY ONE acting card to the seat that
# owns the bounce (``default``, the ops seat -- see the ruling's D4: the reload door is NOT this
# worker's). The standalone daemon loop and the CLI ``dispatch`` entry reach the same gate because
# it sits in :func:`dispatch_once`, the one entry all three share.
#
# DEC-1..DEC-3 (design t_a54c8157, implementation t_ad5dc5d5) shape what that means, and each
# paragraph below is load-bearing:
#
# * DEC-1 -- IDENTITY IS THE EPISODE. The signature is the imported CONTENT alone (no pid) and the
#   window is ``<sig>:<first_tick>``. Two processes on the same content are ONE stall -- one streak,
#   one card -- and a stall that cleared and came back is a NEW window with its own card. The old
#   ``<sig>:<pid>`` signature filed a card per respawn for one stalled revision, and the old
#   ``:<ordinal>`` key swallowed a recurrence whose carrier had been closed.
# * DEC-2 -- THE CARD IS THE RECORD; THE DOOR IS THE ESCALATION REGISTRY. While ``dispatch_once``
#   refuses, nothing on this host is claimed, so a card on a board nobody is serving waits for a
#   human to go looking. The durable row in ``escalations.db`` plus the DM it sends is the door that
#   survives a frozen dispatcher. One attempt per EPISODE, bounded by the record's ``delivery``
#   block, with ``hermes -p default peer run`` as the fallback leg and every failure NAMED.
# * DEC-3 -- RETIREMENT IS AUTOMATIC. The first tick that serves normally again retires the cleared
#   episode's cards through the kernel's own doors (``unblock_task`` then ``complete_task``), with a
#   liveness fence that leaves a live FOREIGN pid's card alone.

#: Greppable prefix of every refusal. A yielding ticker must be distinguishable from a healthy one
#: at the status surface (the ``cron.scheduler.CronTickYielded`` precedent); this string is what a
#: reader greps for.
TICK_YIELD_PREFIX = "tree-skew-yield:"

#: The non-green status record: ``<kanban_home>/kanban/dispatcher_tick_yield.json``, machine-global
#: like the board itself. Tick-scoped on purpose, and a different record from the boot identity
#: (this one records WHY a tick served nothing; the boot record records what the process loaded).
TICK_YIELD_STATUS_FILENAME = "dispatcher_tick_yield.json"
# Version 2 carries the refusal streak (`sig`/`streak`/`first_tick`/`last_tick`) plus, since
# DEC-1..DEC-3 (card t_ad5dc5d5), the EPISODE (`episode`) and the door's single attempt
# (`delivery`: `{at, leg, ref, state, error}`). Version 1 held only the tick's own forensics. The
# reader is tolerant, so a v1 relic on disk reads as "no prior streak" and the next streak simply
# starts at 1; a v2 record whose `sig` carries the retired `:<pid>` suffix likewise reads as a new
# episode, which costs a deployed streak one restart.
TICK_YIELD_STATUS_VERSION = 2

#: Exactly ONE acting card, addressed to the seat that owns the bounce.
TICK_YIELD_ACTOR_ASSIGNEE = "default"
TICK_YIELD_ACTOR_EVENT = "tree_skew_actor_filed"
TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX = "tree-skew:"
TICK_YIELD_ACTOR_TITLE = "[guard] dispatcher is serving a mixed revision; refusing to spawn"

#: The ladder reads a card's OWN declaration line for its blast radius
#: (``kanban_priority_bands.declared_substrate``). A guard card gates every lane on this host and
#: its remedy is the gateway's reload, so it declares exactly that --- which is what places it in
#: the platform tier's TOP sub-band instead of its rest (measured: the same cards were filed at
#: 790000, the rest band, because no declaration was ever written into the body).
TICK_YIELD_ACTOR_DECLARATION = (
    "- Blast radius: gateway runtime + kanban dispatcher (every lane on this host owns the reload)"
)

#: ...and the escalation leg: a skew that SURVIVES a reload is not a reload problem any more.
TICK_YIELD_ESCALATION_ASSIGNEE = "platform-stl"
TICK_YIELD_ESCALATION_TITLE = (
    "[escalation] a gateway reload does not clear the tree skew --- the reload door itself is broken"
)
TICK_YIELD_ESCALATION_KEY_PREFIX = "tree-skew-escalation:"
TICK_YIELD_ESCALATION_EVENT = "tree_skew_escalated"

#: Consecutive refused ticks for ONE skew signature before the guard ACTS. One refusal is normal
#: churn (a tree moves under a daemon mid-write); three in a row is a stall. Ruling t_eaadb60e
#: (2026-10-11): ~3 minutes at the dispatcher's one-refusal-per-minute cadence.
TICK_YIELD_ACTOR_THRESHOLD = 3

#: The ops board the acting card is filed on. The dispatcher may be ticking ANY board, but the ops
#: seat's carrier board is ONE, and filing on the ticked board would mint one card per board for a
#: single fleet-wide stall (different boards are different DBs, so the idempotency key cannot span
#: them). Overridable so an acceptance surface can exercise the path without touching the fleet.
TICK_YIELD_ACTOR_BOARD = os.environ.get("HERMES_KANBAN_TICK_YIELD_BOARD", "migrations")

#: --- DEC-2: the freeze-proof DOOR, and DEC-3: retirement --------------------------------
#: The actor card is the RECORD, not the door. ``dispatch_once`` is the one entry every dispatch
#: path shares, so WHILE IT REFUSES NOTHING IS CLAIMED -- a card filed on a board nobody is serving
#: waits for a human to go looking. The door that survives a frozen dispatcher is therefore the
#: escalation REGISTRY (``escalation_ledger.py``: one durable row in ``escalations.db``) plus the DM
#: it sends. The registry module is loaded BY PATH -- it lives in the platform estate, not on this
#: package's import path -- and a candidate that fails to load is a NAMED failure, never an
#: exception that reaches the tick.
TICK_YIELD_ESCALATION_ENV = "HERMES_KANBAN_ESCALATION_LEDGER"
TICK_YIELD_ESCALATION_CONFIG_KEY = "escalation_ledger"
TICK_YIELD_ESCALATION_DEFAULT = "/opt/hermes_prod/yaan-platform/scripts/escalation_ledger.py"
TICK_YIELD_ESCALATION_DISABLED = ("none", "off")
TICK_YIELD_ESCALATION_SOURCE_KIND = "dispatcher_tree_skew"
TICK_YIELD_ESCALATION_ISSUE_CLASS = "dispatcher-tree-skew"
TICK_YIELD_ESCALATION_RAISED_BY = "kanban-dispatcher"
TICK_YIELD_ESCALATION_TIMEOUT_S = 30

#: Retirement (DEC-3): the event that records an automatic close, and the batch bound. The SELECT
#: is capped so a tick never walks the board: the live backlog was 51 cards, the cap is 100.
TICK_YIELD_RETIRED_EVENT = "tree_skew_actor_retired"
TICK_YIELD_RETIRE_BATCH = 100
#: ``running`` is here for a reason (DEC-3.6): a card under a live claim is somebody's work.
#: ``failed``/``cancelled`` are terminal for the same purpose as ``done``/``archived``.
TICK_YIELD_RETIRE_SKIP_STATUSES = ("done", "archived", "failed", "cancelled", "running")

#: Bounded retirement (card t_028ad743). A live guard that REFUSES the kernel's close used to be
#: re-warned on every tick, forever: three immortal cards on the platform board produced a warning
#: per tick and no action, which is a defect wearing a log line. Every refusal is recorded ON THE
#: CARD, so the bound survives a respawned dispatcher; at the bound ONE ``..._retire_failed`` event
#: names the last refusal (and the shape the kernel will not take) and the card leaves the sweep
#: for good -- it is then an audit finding, not a per-tick siren.
TICK_YIELD_RETIRE_MAX_ATTEMPTS = 3
TICK_YIELD_RETIRE_REFUSED_EVENT = "tree_skew_actor_retire_refused"
TICK_YIELD_RETIRE_FAILED_EVENT = "tree_skew_actor_retire_failed"
#: The ABSORB half (DEC-1.4) is deliberately narrower than the retire half: ``running`` is a LIVE
#: carrier and must absorb the repeat, or a long stall would file a second card beside the first.
TICK_YIELD_ACTOR_ABSORB_SKIP_STATUSES = ("done", "archived", "failed", "cancelled")


#: --- substrate admission (card t_0ae49984, design t_72e9bf03) --------------------------
#: A DIFFERENT condition, the SAME machinery. The tree-skew gate above asks "is this process
#: serving a mixed revision?"; this one asks "can the substrate every spawn needs actually be
#: READ?" --- measured live 2026-10-26: ``/Volumes/yoyofs1`` answered EPERM to ``open(2)`` and
#: ``readdir`` while ``stat``/``exists``/``isdir`` stayed green, so both spawn doors kept
#: admitting work into a volume nobody could read.
#:
#: Reusing the skew gate's episode/streak/card machinery is deliberate (design §3.2: "all
#: inherited from the skew gate --- do not invent a parallel mechanism"). What is NOT shared is
#: the status record: two conditions in one file would overwrite each other's streaks.
TICK_YIELD_KIND_TREE_SKEW = "tree_skew"
SUBSTRATE_ADMISSION_KIND = "substrate_denied"
#: The one greppable string for this condition. ``tree-skew-yield:`` is left byte-identical.
SUBSTRATE_ADMISSION_PREFIX = "substrate-denied-yield:"
SUBSTRATE_ADMISSION_STATUS_FILENAME = "dispatcher_substrate_yield.json"
SUBSTRATE_ADMISSION_ACTOR_IDEMPOTENCY_PREFIX = "substrate-denied:"
SUBSTRATE_ADMISSION_ACTOR_TITLE = "[guard] the substrate is refusing admission --- nothing is being spawned"
SUBSTRATE_ADMISSION_ACTOR_ASSIGNEE = "default"
SUBSTRATE_ADMISSION_HELD_EVENT = "substrate_admission_held"
SUBSTRATE_ADMISSION_ACTOR_EVENT = "substrate_admission_actor_filed"
#: Same shape as the skew declaration, so the ladder bands it in the platform tier's top
#: sub-band: this card gates every lane on the host and its remedy is host administration.
SUBSTRATE_ADMISSION_ACTOR_DECLARATION = (
    "- Blast radius: gateway runtime + kanban dispatcher + cron ticker (every lane on this host)"
)


class DispatcherTickYielded(RuntimeError):
    """The tick refused to serve: this process must not admit work this tick.

    ``kind`` names WHICH condition refused, and with it which strings, record and card the
    refusal uses:

    * ``tree_skew`` (the DEFAULT, so every existing caller and pinned suite is unchanged) ---
      the tree this process imported no longer matches the disk. ``str()`` starts with
      ``tree-skew-yield:``;
    * ``substrate_denied`` --- a ``tier: global`` root denied an operation. ``str()`` starts
      with ``substrate-denied-yield:`` and ``report`` carries the matrix.

    The exception carries the forensics the status record and the acting card quote verbatim.
    """

    def __init__(
        self,
        boot_fingerprint: str,
        disk_fingerprint: str,
        *,
        boot_pid: int,
        kind: str = TICK_YIELD_KIND_TREE_SKEW,
        report: Any = None,
    ) -> None:
        self.kind = str(kind or TICK_YIELD_KIND_TREE_SKEW)
        self.report = report
        self.boot_fingerprint = str(boot_fingerprint)
        self.disk_fingerprint = str(disk_fingerprint)
        self.boot_pid = int(boot_pid)
        self.boot_label = _short_tree_fingerprint(self.boot_fingerprint)
        self.disk_label = _short_tree_fingerprint(self.disk_fingerprint)
        if self.kind == SUBSTRATE_ADMISSION_KIND:
            self.signature = _sa.signature(report) if report is not None else self.boot_fingerprint
            summary = report.summary() if report is not None else "substrate readings unavailable"
            super().__init__(
                f"{SUBSTRATE_ADMISSION_PREFIX} {summary}; refusing to admit work "
                f"(signature {self.signature or 'unknown'}, door dispatcher)."
            )
            return
        self.signature = self.boot_fingerprint
        super().__init__(
            f"{TICK_YIELD_PREFIX} this dispatcher imported tree content {self.boot_label} "
            f"(pid {self.boot_pid}) but the checkout on disk is now {self.disk_label}; refusing "
            f"to spawn from a mixed revision until the gateway is reloaded onto the new code."
        )

    @property
    def matrix(self) -> list:
        """The substrate matrix (``[]`` for a tree-skew refusal)."""
        try:
            return list(self.report.matrix()) if self.report is not None else []
        except Exception:  # noqa: BLE001 - evidence must never raise inside a refusal
            return []


def _short_tree_fingerprint(fingerprint: Optional[str]) -> str:
    """Compact label for a tree fingerprint (degrades to the raw value, never raises)."""
    try:
        from hermes_cli.tree_fingerprint import short

        return short(fingerprint)
    except Exception:  # pragma: no cover - a tree without the module keeps the raw value
        return fingerprint or "unresolved"


def _consumer_tree_skew() -> Optional[tuple[str, str, int]]:
    """``(boot_fingerprint, disk_fingerprint, boot_pid)`` when THIS process is skewed, else None.

    The DECISION is ``hermes_cli.code_skew_content.content_skew`` — the fleet's one staleness
    comparison (card ``t_4dbee32e``). This gate and the carrier's content trigger ask the same
    unit, so they cannot disagree about what "skewed" means; the only difference between them is
    WHO is asking, which is exactly what makes the pair work: this check lives inside the frozen
    process it judges, and the carrier's lives in a fresh one.

    CONTENT first, and load-bearing: the working tree is what moved in the incident this closes.
    The git-ref check is consulted only when the content half reports UNKNOWN, so a pure HEAD move
    whose files are byte-identical is still caught — the ref path can ADD detection but never
    replace it.

    Every uncertain read returns ``None`` (no boot record, unreadable tree, unchanged tree): the
    gate fails OPEN, exactly like the ref-only guard it backstops. A guard that refused on an
    unreadable read would stop the fleet on a transient IO error. (An unknown read is NOT a claim
    of health — the carrier's trigger is what turns it into a loud state, and ``None`` never
    reaches a card as "clear".)
    """
    try:
        from hermes_cli import code_skew_content, tree_fingerprint

        record = tree_fingerprint.boot_record() or {}
        measured = code_skew_content.measure(os.getpid(), record.get("tree"))
    except Exception:
        measured, record = {}, {}
    boot_pid = int(record.get("pid") or os.getpid())
    if measured.get("skew") is True:
        return str(measured["boot"]), str(measured["disk"]), boot_pid

    try:
        from gateway import code_skew

        ref_skew = code_skew.detect_code_skew()
    except Exception:
        ref_skew = None
    if ref_skew is None:
        return None
    boot_fingerprint = record.get("fingerprint") or f"git-ref:{ref_skew[0]}"
    return str(boot_fingerprint), f"git-ref:{ref_skew[1]}", boot_pid


def _raise_if_tree_skewed() -> None:
    """Refuse the tick -- raise :class:`DispatcherTickYielded` -- when this process is provably
    serving a mixed revision. The named entry point of the gate."""
    skew = _consumer_tree_skew()
    if skew is None:
        return
    raise DispatcherTickYielded(skew[0], skew[1], boot_pid=skew[2])


def tick_yield_status_path(path: Any = None) -> Path:
    """The non-green status record's path, or the explicit one a caller or a test supplies."""
    if path is not None:
        return Path(path)
    return _kb.kanban_home() / "kanban" / TICK_YIELD_STATUS_FILENAME


def substrate_admission_status_path(path: Any = None) -> Path:
    """The substrate refusal's OWN status record (design §3.2: a sibling, never shared).

    Two conditions in one file would overwrite each other's streaks, so the substrate record is
    ``<kanban_home>/kanban/dispatcher_substrate_yield.json``: same schema v2, plus ``matrix``.
    """
    if path is not None:
        return Path(path)
    return _kb.kanban_home() / "kanban" / SUBSTRATE_ADMISSION_STATUS_FILENAME


def _tick_yield_status_path_for(yielded: DispatcherTickYielded) -> Path:
    """The record this refusal writes: per KIND, so neither condition can clobber the other."""
    if getattr(yielded, "kind", TICK_YIELD_KIND_TREE_SKEW) == SUBSTRATE_ADMISSION_KIND:
        return substrate_admission_status_path()
    return tick_yield_status_path()


def _yield_signature(yielded: DispatcherTickYielded) -> str:
    """The EPISODE's content: the tree content this process IMPORTED, and nothing else.

    DEC-1 (ruling ``t_a54c8157``, card ``t_ad5dc5d5``): identity is per-EPISODE, not per-process.
    ``boot_pid`` is deliberately NOT here. Two processes refusing on the same imported content are
    ONE stalled revision -- the reload that clears them is the same reload -- so a pid-sensitive
    signature would start a fresh streak on every respawn and file a card per process for a single
    stall. The pid stays EVIDENCE everywhere it already appears (the record's ``pid``, the card
    body, the event payload); identity is the content, and the re-open door is the WINDOW
    (:func:`_episode_identity`): a stall that cleared and came back is a new window and gets its
    own card.

    The disk half is not in the signature either. Bytes landing under a frozen process change the
    disk side without ending the stall, so a disk-sensitive identity would reset the streak on
    every landing and file a second card mid-stall. The disk fingerprint is EVIDENCE (status record
    + card body), never identity.

    KIND is the one branch: a tree-skew refusal's identity is the imported content (unchanged,
    byte-compatible); a substrate refusal's identity is ``substrate_access.signature`` --- a
    ``sha256`` over the sorted ``path\\top\\terrno`` of every DENIED reading, so it aggregates by
    CAUSE, not by count. The exception carries both, so this function re-derives neither.
    """
    return str(yielded.signature)


def _episode_identity(signature: str, first_tick: int) -> str:
    """The EPISODE's identity: the imported content plus the window's START.

    ``first_tick`` is the streak's own first refusal (the record's ``first_tick``), so the identity
    is stable for the whole window however many ticks -- and however many processes -- refuse
    inside it, and changes the moment the window re-opens.
    """
    return f"{signature}:{int(first_tick)}"


def _read_tick_yield_status(path) -> dict:
    """The status record as a dict, or ``{}``. Tolerant by design: an unreadable record is "no
    prior streak", never a refusal -- a guard that stopped the fleet on a transient IO error would
    be worse than the condition it watches."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write_tick_yield_status(payload: dict, path=None) -> str:
    """Write the status record verbatim and return its path (or the named reason it could not be).

    Best-effort by design -- an unwritable kanban home must not take the tick down -- but never
    silent: the refusal has already been logged loudly by the caller, and the return value is
    carried into the log line and the acting card so a reader can find the record even when it
    could not be written.
    """
    target = tick_yield_status_path(path)
    try:
        from utils import atomic_json_write

        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(target, payload, mode=0o600)
        return str(target)
    except Exception as exc:
        _kb._log.warning(
            "kanban dispatcher: could not write the tree-skew status record to %s: %s", target, exc,
        )
        return f"(status record unwritable: {exc})"


def _record_tick_yield(
    yielded: DispatcherTickYielded, *, board: Optional[str] = None,
) -> tuple[str, dict]:
    """Write the NON-GREEN status record, carrying the REFUSAL STREAK. ``(path, payload)``.

    How many consecutive refused ticks have produced this same IMPORTED CONTENT, and the window's
    first and last tick. Corroboration only -- the streak is the dispatcher's OWN refusal count,
    never the reload state row's counter (whose ``consecutive: 10`` sat beside ``act: none, card:
    <closed>``).

    DEC-1.2: the streak continues across PROCESSES. ``same`` compares the signature only, so a
    respawn -- or a second dispatcher process on the same frozen content -- continues the window
    and keeps ``first_tick`` instead of starting its own. The pid is still written, as EVIDENCE.

    DEC-2.4: the delivery attempt is PER EPISODE and BOUNDED -- once the record carries a
    ``delivery`` for THIS episode it is carried forward untouched, so a fourth, fifth and fiftieth
    refusal cannot re-register, re-notify or re-file. The streak is recomputed every tick; the
    delivery is not.
    """
    path = _tick_yield_status_path_for(yielded)
    prior = _read_tick_yield_status(path)
    now = int(time.time())
    sig = _yield_signature(yielded)
    same = bool(prior.get("yielded")) and prior.get("sig") == sig
    if same:
        streak = int(prior.get("streak") or 0) + 1
        first_tick = int(prior.get("first_tick") or now)
    else:
        streak = 1
        first_tick = now
    episode = _episode_identity(sig, first_tick)
    delivery = prior.get("delivery") if prior.get("episode") == episode else None
    payload = {
        "version": TICK_YIELD_STATUS_VERSION,
        "yielded": True,
        "reason": str(yielded),
        "boot_fingerprint": yielded.boot_fingerprint,
        "disk_fingerprint": yielded.disk_fingerprint,
        "boot_label": yielded.boot_label,
        "disk_label": yielded.disk_label,
        "pid": yielded.boot_pid,
        "board": board,
        # --- the streak half (version 2) ---
        "sig": sig,
        "streak": streak,
        "first_tick": first_tick,
        "last_tick": now,
        "threshold": TICK_YIELD_ACTOR_THRESHOLD,
        # --- the episode half (DEC-1.3 / DEC-2.4) ---
        "episode": episode,
        "delivery": delivery,
        "recorded_at": now,
    }
    if getattr(yielded, "kind", TICK_YIELD_KIND_TREE_SKEW) == SUBSTRATE_ADMISSION_KIND:
        # The substrate half: the SAME v2 schema, plus what a reader needs to see the cause
        # without opening anything else. The skew record is left byte-identical.
        payload["kind"] = SUBSTRATE_ADMISSION_KIND
        payload["matrix"] = yielded.matrix
    return _write_tick_yield_status(payload, path), payload


def _merge_tick_yield_delivery(delivery: dict) -> str:
    """Record a delivery attempt on the CURRENT episode's status record.

    Deliberately NOT routed through :func:`_record_tick_yield`: this must not touch the streak (the
    attempt happens inside one refusal, and a second write that re-derived the streak would count
    the same tick twice). A record that vanished or names another episode is left alone and the
    caller's return value says so.
    """
    path = tick_yield_status_path()
    record = _read_tick_yield_status(path)
    if not record.get("yielded"):
        return "(no status record to carry the delivery)"
    record["delivery"] = delivery
    return _write_tick_yield_status(record, path)


def _clear_tick_yield_status(
    kind: str = TICK_YIELD_KIND_TREE_SKEW, *, released: Optional[list] = None,
) -> bool:
    """Retire a stale non-green marker once a tick serves normally again. ``True`` when retired.

    Cleared ONLY when this process wrote the record (it is the one that is now healthy), or when
    the recording process is PROVEN dead (its refusal is history, not a live condition). A fresh
    dispatcher process must never clear a live gateway's marker -- that would read green over a
    frozen gateway, which is the exact false green this record exists to prevent.

    ``kind`` selects WHICH record: the skew sibling, or the substrate sibling. For the substrate
    the retirement ALSO writes the RECOVERY RECEIPT onto the authority record
    (``substrate_access.record_cleared``: ``cleared_at``, ``duration_s``, the cleared matrix, the
    units released) -- design §3.7's proof that the episode is over, not merely unobserved.

    The return value is DEC-3's trigger: a tick that just retired a record is a tick whose episode
    CLEARED, and that is the moment the actor cards filed for it are retired too.
    """
    substrate = str(kind) == SUBSTRATE_ADMISSION_KIND
    path = substrate_admission_status_path() if substrate else tick_yield_status_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    try:
        record = json.loads(raw)
    except (ValueError, TypeError):
        record = None
    if isinstance(record, dict):
        recorded_pid = record.get("boot_pid" if substrate else "pid")
        try:
            recorded_pid = int(recorded_pid)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            recorded_pid = None
        if recorded_pid is not None and recorded_pid != os.getpid():
            if _pid_liveness(recorded_pid) != WORKER_DEAD:
                return False
    if substrate:
        try:
            _sa.record_cleared(door="dispatcher", released=list(released or []))
        except Exception:  # health: allow BLE001 -- the receipt is evidence, never a tick's work
            _kb._log.warning(
                "kanban dispatcher: could not write the substrate recovery receipt", exc_info=True,
            )
    try:
        path.unlink()
    except OSError:
        pass
    return True


def _last_spawned_line(board: Optional[str], *, limit_bytes: int = 2_000_000) -> str:
    """The last successful dispatcher tick summary for `board`, or a NAMED reason there is none.

    The ruling's own evidence form: `kanban dispatcher [<board>]: spawned=1 reclaimed=0 ...`
    in `<HERMES_HOME>/logs/gateway.log`. Read from the TAIL of the file only (`limit_bytes`) so the
    read is O(tick), never O(log). An unreadable/absent log answers with the reason, never a silent
    empty string -- a card that omits this line would read as "nothing was ever spawned", which is
    a different claim from "I could not read the log".
    """
    import re

    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    path = Path(home) / "logs" / "gateway.log"
    board_name = re.escape(str(board or ""))
    pattern = re.compile(rf"kanban dispatcher \[{board_name}\]: spawned=")
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > limit_bytes:
                fh.seek(size - limit_bytes)
                fh.readline()  # drop the partial first line
            tail = fh.read().decode("utf-8", "replace")
    except OSError as exc:
        return f"(gateway log unreadable: {exc})"
    last = ""
    for line in tail.splitlines():
        if pattern.search(line) and "spawned=" in line:
            last = line.strip()
    if not last:
        return "(no successful spawn line for this board in the log tail)"
    return last


def _tree_skew_subject_root() -> Path:
    """The tree the gate reasoned about: the boot record's tree when it names one, else ours.

    In production the two are the same install; naming the RECORDED tree keeps the card honest when
    a caller points the fingerprint at another root (a scratch-tree demonstration), so a reader
    never has to reconcile the evidence with the wrong checkout.
    """
    try:
        from hermes_cli import tree_fingerprint

        named = (tree_fingerprint.boot_record() or {}).get("tree")
        if named:
            return Path(str(named))
    except Exception:
        pass
    return _dispatching_tree_root()


def _tick_yield_actor_body(
    yielded: DispatcherTickYielded,
    *,
    board: Optional[str],
    status_path: str,
    streak: int = 0,
    first_tick: int = 0,
    last_tick: int = 0,
    tree: Optional[Path] = None,
    filed_on: Optional[str] = None,
    episode: str = "",
    escalation_ref: Optional[str] = None,
) -> str:
    """The acting card's body: deterministic evidence, no model, no prose to interpret."""
    tree = Path(tree) if tree is not None else _tree_skew_subject_root()
    import datetime

    def _iso(epoch):
        try:
            return datetime.datetime.fromtimestamp(int(epoch)).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            return "?"

    escalation = (
        f"`{escalation_ref}`" if escalation_ref
        else "`(none filed -- the escalation registry was unavailable this tick; see the status "
             "record's `delivery` block for the named failure)`"
    )

    lines = [
        TICK_YIELD_ACTOR_DECLARATION,
        "",
        "## Observed",
        "",
        f"`{TICK_YIELD_PREFIX}` the kanban dispatcher is refusing to spawn: the tree it IMPORTED",
        "no longer matches the checkout on disk, so every worker it spawns would run a mixed",
        "revision. Filed by the dispatcher's tick skew gate -- deterministic, no model consulted.",
        "",
        "## The refusal streak",
        "",
        f"- consecutive refused ticks: **{streak}** (act threshold {TICK_YIELD_ACTOR_THRESHOLD})",
        f"- first tick: `{_iso(first_tick)}` (epoch {int(first_tick)})",
        f"- last tick: `{_iso(last_tick)}` (epoch {int(last_tick)})",
        f"- frozen process pid: {yielded.boot_pid}",
        "",
        "## The measurement",
        "",
        f"- imported tree content (what the running process loaded): `{yielded.boot_fingerprint}`"
        f" (label `{yielded.boot_label}`)",
        f"- on-disk checkout (what the tree holds now): `{yielded.disk_fingerprint}`"
        f" (label `{yielded.disk_label}`)",
        f"- tree: `{tree}`",
        f"- board refused: `{board or 'unknown'}`",
        f"- card filed on: `{filed_on or board or 'unknown'}`",
        f"- episode: `{episode or 'unknown'}`",
        f"- escalation: {escalation}",
        f"- non-green status record: `{status_path}`",
        f"- last successful spawn: `{_last_spawned_line(board)}`",
        "",
        "## Why this is not the ref-only signal",
        "",
        "`gateway.code_skew.detect_code_skew()` fingerprints only `.git/HEAD` -> ref -> sha and",
        "never reads the working tree, so it reports NO skew while a working-tree rewrite is in",
        "progress under an unchanged HEAD -- measured live 2026-10-10 at 120 dirty paths with",
        "`detect_code_skew() == None`. The signal above is content, so that window is covered.",
        "",
        "## DEADLINE -- 30 minutes",
        "",
        "This card IS the deadline's carrier: a `tree-skew-yield` refusal is MONOTONIC (it clears",
        "only on a reload) and it stops dispatch for every lane on this host. Take the reload within",
        "30 minutes of the last tick above, or comment why not.",
        "",
        "## This card is the RECORD, not the remedy",
        "",
        "The remedy is the RELOAD: the sanctioned door is the ops seat's `bounce_gateway` verb",
        "(`default` / `platform-stl`), with `force=true` and a note when the lull gate cannot clear",
        "-- the card-worker hold is ADVISORY and must never gate a service restore. The hand form is",
        "the canonical planned-stop marker for the pid plus ONE SIGTERM (launchd KeepAlive respawns",
        "it); a hand reload must write its `bounces.jsonl` row through the deploy seam or it is",
        "unattributable.",
        "",
        f"The DOOR is the escalation row above ({escalation}) plus the DM it sends to the ops seat.",
        "While `dispatch_once` refuses, NOTHING on this host is claimed, so a card on a board nobody",
        "is serving is a record a human has to go looking for -- do NOT \"claim this card\" as the",
        "remedy, and do not close it by hand: leaving it open is what keeps the stall readable.",
        "",
        "Retirement is AUTOMATIC (DEC-3): the first tick that serves normally again retires every",
        "actor card of the cleared episode (`tree_skew_actor_retired`), because a serving tick is",
        "proof the episode is over. A card whose filing pid is still a LIVE foreign process is left",
        "alone -- a frozen peer still holds that episode.",
        "",
        "```",
        *_git_status_lines(tree),
        "```",
    ]
    return "\n".join(lines)


def _tick_yield_actor_priority(
    *, title: str, body: str, board: str, assignee: Optional[str] = None,
) -> tuple[int, str]:
    """(priority, note) asked of the LADDER, never typed.

    Resolved through the board's OWN priority policy (`board.json:priority_policy`), loaded the
    same way the create door loads it, with GATE_ASK (0): the card is born where the ladder places
    it. An unavailable ladder is RECORDED, never invented away -- the card is still filed, at the
    kernel default, and the note rides into the card body.

    The BODY is passed in full because the ladder's qualification axis is the card's own
    declaration line (``declared_substrate``): handing it ``body=""`` files the card in the tier's
    rest band however load-bearing the card is.
    """
    try:
        from hermes_cli import kanban_priority_policy as _pol

        meta = _kb._board_meta_for(board) or {}
        verdict = _pol.priority_for_create(
            0, assignee=assignee or TICK_YIELD_ACTOR_ASSIGNEE, board=board, title=title,
            body=body, spec=meta.get("priority_policy"),
        )
        if verdict is None:
            return 0, (f"(no priority policy on board {board}: filed at the kernel default; "
                       f"the board sweep will band it)")
        return int(verdict.applied), ""
    except Exception as exc:
        return 0, f"(the ladder could not be read: {exc.__class__.__name__}: {exc})"


def _ops_board_connection(fallback_conn: sqlite3.Connection):
    """(conn, board, close_it) for the board the acting card rides.

    The ops board when it can be opened, else the board that was ticking -- and the caller SAYS
    which, in the card, so a fallback is visible rather than silent. The fallback's slug is read
    back off the connection (never assumed) so the card cannot name a board it is not on.
    """
    try:
        conn = _kbc.connect(board=TICK_YIELD_ACTOR_BOARD)
        return conn, TICK_YIELD_ACTOR_BOARD, True
    except Exception as exc:
        _kb._log.warning(
            "kanban dispatcher: cannot open the ops board %s for the tree-skew actor (%s); "
            "filing on the board that ticked", TICK_YIELD_ACTOR_BOARD, exc,
        )
        try:
            fallback_board = _kb.board_for_connection(fallback_conn)
        except Exception:
            fallback_board = None
        return fallback_conn, fallback_board, False


def _record_rows(record: Any, *keys: str) -> list[dict]:
    """The dict rows under the first of ``keys`` that actually HAS rows; anything else dropped.

    The authority record is written by ANOTHER process, so its shape is a claim to be checked,
    not trusted: this runs inside a live refusal, where an exception costs exactly the card
    that explains the stall (see :func:`_blind_guard_count`). An EMPTY list is not an answer --
    a LEG A record carries ``guards: []``-shaped nothing and its rows in ``guards_blind``.
    """
    for key in keys:
        value = record.get(key) if isinstance(record, dict) else None
        if isinstance(value, (list, tuple)):
            rows = [row for row in value if isinstance(row, dict)]
            if rows:
                return rows
    return []


def _blind_guard_count(record: Any) -> int:
    """The blind-guard COUNT, tolerant of BOTH shapes the record has carried.

    Leg B writes ``guards_blind`` as an int and keeps the rows in ``guards``. The LEG A build
    deployed between the two wrote the ROW LIST in ``guards_blind`` -- and a bare ``int()`` on
    that list raised TypeError inside the refusal path on 2026-10-26, so the hold was recorded
    (matrix, episode, streak) and NO card carried it: 33 units queued behind a hold no reader
    could see. A count is a count; a row list is its length.
    """
    value = record.get("guards_blind") if isinstance(record, dict) else None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        return len(value)
    return 0


#: At most this many ROOTS are named on one held row; the count is always exact and the elision
#: is printed as ``+N more``. Same bound as the mirror's ``substrate_view.RENDERED_ROOTS`` so the
#: card and the dashboard never disagree about how much of a root list they will show.
HELD_ROW_ROOTS = 8

#: At most this many characters of ONE value on a held row (paths, names, causes).
HELD_ROW_VALUE_CHARS = 120

#: The keys a held row may carry its OWNER under -- one per writer, and the writers differ.
HELD_ROW_IDENTITY_KEYS = ("unit", "task", "job")

#: The keys a held row may carry its LOCATION under: the dispatcher names one ``root``, the cron
#: door names a list of ``roots``.
HELD_ROW_LOCATION_KEYS = ("root", "roots")

#: The keys that are the CAUSE, in the order the row reads them.
HELD_ROW_CAUSE_KEYS = ("errno", "op", "reason")


def _record_entries(record: Any, *keys: str) -> list:
    """The RAW entries under the first of ``keys`` that is a non-empty list/tuple; NOTHING dropped.

    :func:`_record_rows` keeps dict rows only, on purpose -- the row renderer must never raise on
    a shape it cannot read. But a COUNT taken from those filtered rows then states fewer units
    than the record names, which is the same false-statement fault this body exists to remove
    (``held: [{...}, "not-a-dict"]`` counted 1 and rendered that 1 as ``None``; card t_0b56c6a8).
    A row this reader cannot parse is still a HELD unit, so the count and the row loop come from
    HERE, and every entry -- dict or not -- goes through :func:`_held_row_line`.
    """
    for key in keys:
        value = record.get(key) if isinstance(record, dict) else None
        if isinstance(value, (list, tuple)) and value:
            return list(value)
    return []


def _held_absent(value: Any) -> bool:
    """True when a value carries nothing worth printing -- and never raises deciding it.

    ``value not in (None, "")`` reads harmless, but the record is JSON-decoded by ANOTHER
    process and a value with a broadcasting ``==`` (an array-like) makes that test raise inside
    the refusal path. ``0`` and ``False`` are PRESENT: ``overdue_s=0`` is a fact, not a blank.
    """
    if value is None:
        return True
    if isinstance(value, (str, list, tuple, dict)):
        return len(value) == 0
    return False


def _held_text(value: Any, limit: int = HELD_ROW_VALUE_CHARS) -> str:
    """ONE bounded, single-line spelling of a record value. Never raises.

    ``repr`` of an arbitrary object is a claim to be survived here, not trusted: this runs inside
    a live refusal, where an exception costs exactly the card that explains the stall.
    """
    try:
        text = value if isinstance(value, str) else repr(value)
    except Exception:  # noqa: BLE001 -- an unrenderable value is still a held unit
        text = f"<unrenderable {type(value).__name__}>"
    text = " ".join(str(text).split())
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _held_location_text(row: dict) -> tuple[str, str]:
    """``(key, text)`` for the row's location, or ``("", "")`` when it names none.

    The location is printed with the key the row ACTUALLY used, so ``root=/a`` and
    ``roots=/a, /b`` are both readable and neither is invented.
    """
    for key in HELD_ROW_LOCATION_KEYS:
        value = row.get(key)
        if _held_absent(value):
            continue
        if isinstance(value, (list, tuple)):
            names = [_held_text(one) for one in value]
            names = [name for name in names if name]
            if not names:
                continue
            if len(names) > HELD_ROW_ROOTS:
                names = names[:HELD_ROW_ROOTS] + [f"+{len(names) - HELD_ROW_ROOTS} more"]
            return key, ", ".join(f"`{name}`" for name in names)
        return key, f"`{_held_text(value)}`"
    return "", ""


def _held_row_line(item: Any) -> str:
    """ONE legible line for a HELD unit, built from the keys that row ACTUALLY carries.

    The shape differs BY WRITER and always will: the dispatcher's door 3 counts a CARD
    (``{task, root, errno, op}``) and the cron door 3 counts a JOB
    (``{job, name, roots, errno, op, overdue_s}``). One record does NOT mix the two today --
    ``substrate_access.record`` writes ``list(held or prior.get("held") or [])``, so a non-empty
    ``held`` REPLACES the prior list, and the only carry-forward path (a writer passing no
    ``held``) reproduces the prior rows verbatim. The per-row read is bought by the WRITER axis,
    not by a mixed record: each door's rows are homogeneous today, and a future writer's rows may
    differ from both. The body used to read ``unit``/``kind``/``reason``, keys no writer has ever
    emitted, and rendered every row as ``None (None): None``: the count line was right and every
    row under it named nothing (card t_0b56c6a8).

    So each row is read on its own: identity (``unit`` | ``task`` | ``job``, plus ``name``),
    location (``root`` | ``roots``, capped, elision counted), cause (``errno``, ``op``,
    ``reason``), then EVERY remaining key as ``key=value`` -- which makes an unrecognised shape
    legible instead of blank, and a key added by a future writer visible without another card.
    A row that is not a dict is rendered as itself rather than dropped.
    """
    if not isinstance(item, dict):
        return f"  - (unrecognised held row, {type(item).__name__}): {_held_text(item)}"
    row = dict(item)
    identity = next(
        (key for key in HELD_ROW_IDENTITY_KEYS if not _held_absent(row.get(key))), None
    )
    if identity is None:
        pairs = ", ".join(f"{key}={_held_text(row[key])}" for key in sorted(row, key=str))
        return f"  - (unrecognised held row): {pairs or '(empty)'}"
    label = identity
    name = row.pop("name", None)
    if not _held_absent(name):
        label = f'{identity} "{_held_text(name)}"'
    segments = [f"  - `{_held_text(row.pop(identity))}` ({label})"]
    location_key, location = _held_location_text(row)
    if location_key:
        # Only the key that WON is consumed: a row carrying both `root` and `roots` keeps the
        # other one visible below rather than having it dropped for being second.
        row.pop(location_key, None)
        segments.append(f"{location_key}={location}")
    for key in HELD_ROW_CAUSE_KEYS:
        value = row.pop(key, None)
        if not _held_absent(value):
            segments.append(f"{key}={_held_text(value)}")
    for key in sorted(row, key=str):
        value = row[key]
        if _held_absent(value):
            continue
        segments.append(f"{key}={_held_text(value)}")
    return " ".join(segments)


def _substrate_admission_body(
    yielded: DispatcherTickYielded,
    *,
    board: Optional[str],
    status_path: str,
    streak: int = 0,
    first_tick: int = 0,
    last_tick: int = 0,
    filed_on: Optional[str] = None,
    episode: str = "",
) -> str:
    """The SUBSTRATE acting card's body: deterministic evidence, no model, no prose to interpret.

    Design §3.2 asks for four things and nothing else: the MATRIX, the affected UNITS, the
    ``guards_blind`` count, and the REMEDY -- bounded to host administration, so the reader is
    never left choosing between a card-level retry and a host-level repair.
    """
    import datetime

    def _iso(epoch):
        try:
            return datetime.datetime.fromtimestamp(int(epoch)).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            return "?"

    record = _sa.read_status()
    if not isinstance(record, dict):  # read_status never raises; a stub caller might still lie
        record = {}
    # LEG A carried the blind ROWS in `guards_blind`; a record written by that build has no
    # `guards` key at all, so both keys are consulted and the rows are read from whichever
    # holds a list. The count comes from `_blind_guard_count` for the same reason.
    matrix = yielded.matrix or (record.get("matrix") or [])
    # The COUNT and the rows both come from the raw list: a held row the reader cannot parse is
    # still a held unit, and a count that hides it is the fault this body removes (t_0b56c6a8).
    held = _record_entries(record, "held")
    blind = _record_rows(record, "guards", "guards_blind")
    blind_count = _blind_guard_count(record)
    blind_total = record.get("guards_total")

    lines = [
        SUBSTRATE_ADMISSION_ACTOR_DECLARATION,
        "",
        "## Observed",
        "",
        f"`{SUBSTRATE_ADMISSION_PREFIX}` the kanban dispatcher is refusing to spawn: a root every worker",
        "needs answered a DENIAL to a real operation (`open(2)` / `readdir` / `O_CREAT`), so any",
        "worker admitted now would be born into a substrate nobody can read. Filed by the",
        "substrate admission gate -- deterministic, no model consulted.",
        "",
        "READ THIS FIRST: `stat`/`exists`/`isdir` stay GREEN through this fault (measured",
        "2026-10-26), so a check that only stats the path proves nothing either way.",
        "",
        "## The refusal streak",
        "",
        f"- consecutive refused ticks: **{streak}** (act threshold {TICK_YIELD_ACTOR_THRESHOLD})",
        f"- first tick: `{_iso(first_tick)}` (epoch {int(first_tick)})",
        f"- last tick: `{_iso(last_tick)}` (epoch {int(last_tick)})",
        f"- episode: `{episode or 'unknown'}` (signature `{yielded.signature or 'unknown'}`)",
        "",
        "## The matrix",
        "",
    ]
    if matrix:
        for row in matrix:
            lines.append(
                f"- `{row.get('path')}` op={row.get('op')} **{row.get('errno')}** tier={row.get('tier')}"
            )
    else:
        lines.append("- (no matrix on the record -- the refusal carried no readings)")
    lines += [
        "",
        "## The affected units",
        "",
        f"- units held by per-unit admission this episode: **{len(held)}**",
    ]
    for item in held[:20]:
        lines.append(_held_row_line(item))
    if len(held) > 20:
        lines.append(f"  - (+{len(held) - 20} more held unit(s) not shown)")
    lines += [
        "",
        "## Guards blind (fail-open hooks)",
        "",
        f"- **{blind_count} of {blind_total if blind_total is not None else '?'} "
        "launcher-armed hook row(s) are failing OPEN** — observation is lost, and nothing is "
        "being blocked by them either. Counted, never silent: a fail-open nobody reads is a "
        "silent failure.",
    ]
    for item in blind[:5]:
        lines.append(
            f"  - `{item.get('since')}` `{item.get('hook')}` row `{item.get('row')}`: "
            f"{item.get('reason')} -> `{item.get('body')}`"
        )
    lines += [
        "",
        "## Where the evidence lives",
        "",
        f"- authority record (boot volume): `{_sa.status_path()}`",
        f"- dispatcher tick record: `{status_path}`",
        f"- board refused: `{board or 'unknown'}`",
        f"- card filed on: `{filed_on or board or 'unknown'}`",
        "",
        "## What to do -- host administration, not a card retry",
        "",
        "1. Restore READ access on the roots named above (remount / reconnect the volume, or fix",
        "   the permissions). Nothing on this board is waiting on a decision: no card was failed,",
        "   blocked or failure-counted, and no workspace was created (invariant I1).",
        "2. Confirm with the probe itself, not with `ls` or `stat`:",
        "   `python3 -c \"from hermes_cli import substrate_access as s; print(s.probe_all(use_cache=False).matrix())\"`",
        "   -- every row must say `ok`. `stat` answers `ok` during this fault and proves nothing.",
        "3. The next healthy tick closes this card on its own and writes the recovery receipt",
        "   (`cleared_at`, `duration_s`, the cleared matrix) into the authority record. Nothing",
        "   needs to be re-filed or re-spawned by hand.",
    ]
    return "\n".join(lines)


def _file_tick_yield_actor(
    conn: sqlite3.Connection,
    yielded: DispatcherTickYielded,
    *,
    board: Optional[str] = None,
    status_path: str = "",
    streak: int = 0,
    first_tick: int = 0,
    last_tick: int = 0,
    window_start: int = 0,
    escalation_ref: Optional[str] = None,
    now: Optional[int] = None,
) -> Optional[str]:
    """File the ACTING card for this episode, idempotently and status-aware.

    Key shape: ``tree-skew:<signature>:<window_start>`` (DEC-1.4) -- the WINDOW replaces both the
    pid and the old ``:<ordinal>``. Two processes on the same imported content therefore reach the
    same key, and one stall is one card however often its dispatcher respawns.

    * at most ONE OPEN actor per imported content: every task on the actor board whose key starts
      ``tree-skew:<signature>:`` and whose status is not terminal absorbs the repeat, so the
      cardinality holds EVEN when the status record could not be read or written (the lookup is on
      the board, not on the record). LEGACY keys (``tree-skew:<sig>:<pid>:<ord>``, 78 of them live)
      match the same prefix and absorb too;
    * a CLEARED window is not absorbed: its carrier is ``done`` (terminal), and the next window
      mints a NEW key from its own ``first_tick`` -- the fixed-key defect that cost 1h38m on
      2026-10-11 is exactly what the window replaces.

    Best-effort: a board that refuses the write is logged with the key it could not file and never
    takes the tick down.
    """
    substrate = getattr(yielded, "kind", TICK_YIELD_KIND_TREE_SKEW) == SUBSTRATE_ADMISSION_KIND
    sig = _yield_signature(yielded)
    # KIND-scoped key, title, assignee and event: a substrate episode and a skew episode can never
    # absorb each other's cards, and each retirement sweep selects only its own.
    prefix = (
        f"{SUBSTRATE_ADMISSION_ACTOR_IDEMPOTENCY_PREFIX if substrate else TICK_YIELD_ACTOR_IDEMPOTENCY_PREFIX}"
        f"{sig}"
    )
    actor_title = SUBSTRATE_ADMISSION_ACTOR_TITLE if substrate else TICK_YIELD_ACTOR_TITLE
    actor_assignee = SUBSTRATE_ADMISSION_ACTOR_ASSIGNEE if substrate else TICK_YIELD_ACTOR_ASSIGNEE
    actor_event = SUBSTRATE_ADMISSION_ACTOR_EVENT if substrate else TICK_YIELD_ACTOR_EVENT
    filed_on_conn, filed_on, close_it = _ops_board_connection(conn)
    try:
        # A skew that SURVIVED a reload is not a reload problem: file ONE escalation and stop
        # minting duplicates of the same demand (card t_4dbee32e; the 116-card pile). Asked
        # BEFORE the absorb, deliberately: DEC-1.4's key is per EPISODE, so a guard card filed by
        # the process that has since been reloaded absorbs every later tick -- an absorb-first
        # order would make this branch dead code and keep re-filing a demand a reload is already
        # known not to clear.
        #
        # WHAT COUNTS AS SURVIVAL (card t_5a54ca29, R-1): the SAME IMPORTED CONTENT, filed by a
        # process that is now proven dead, on a card that was not already cleared before this
        # process booted. The disk fingerprint is evidence, never the identity: it is the one
        # fingerprint a reload does not change, so matching it turned a re-skew onto a revision
        # the fleet had already left (b1b9cb6 -> 47558cf, five minutes) into an "escalation" that
        # cited a guard card closed long before. The escalation is idempotent per EPISODE, so
        # every later refusal of the same surviving skew returns the same card.
        survived = None if substrate else _skew_survived_a_reload(filed_on_conn, yielded)
        if survived is not None:
            return _file_tick_yield_escalation(
                filed_on_conn, yielded, board=board, filed_on=filed_on,
                status_path=status_path, streak=streak, prior=survived,
                first_tick=first_tick,
            )
        placeholders = ",".join("?" for _ in TICK_YIELD_ACTOR_ABSORB_SKIP_STATUSES)
        existing = filed_on_conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key LIKE ? || ':%' "
            f"AND status NOT IN ({placeholders}) ORDER BY rowid LIMIT 1",
            (prefix, *TICK_YIELD_ACTOR_ABSORB_SKIP_STATUSES),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        key = f"{prefix}:{int(window_start)}"

        if substrate:
            body = _substrate_admission_body(
                yielded, board=board, status_path=status_path, streak=streak,
                first_tick=first_tick, last_tick=last_tick, filed_on=filed_on,
                episode=_episode_identity(sig, window_start or first_tick),
            )
        else:
            body = _tick_yield_actor_body(
                yielded, board=board, status_path=status_path, streak=streak,
                first_tick=first_tick, last_tick=last_tick, filed_on=filed_on,
                episode=_episode_identity(sig, window_start or first_tick),
                escalation_ref=escalation_ref,
            )
        # The ladder reads the card's OWN declaration line, so the body is built FIRST: asking it
        # about an empty body is how these cards were born in the tier's rest band (790000) while
        # declaring nothing at all.
        priority, ladder_note = _tick_yield_actor_priority(
            title=actor_title, body=body, board=filed_on or board or "",
        )
        if ladder_note:
            body += f"\n\n_priority note: {ladder_note}_"
        # BORN READY, never `scheduled`: the kernel resolves a parentless, non-triage create to
        # `ready` (kanban_db_graph.initial_task_state) -- the state a deadline carrier must be in
        # for a seat to be dispatched against it. Pinned by test_guard_card_is_ready_and_top_band.
        actor_id = _kb.create_task(
            filed_on_conn,
            title=actor_title,
            body=body,
            assignee=actor_assignee,
            created_by="kanban-dispatcher",
            priority=priority,
            idempotency_key=key,
        )
        with _kb.write_txn(filed_on_conn):
            _kb._append_event(
                filed_on_conn, actor_id, actor_event,
                {
                    "kind": yielded.kind,
                    "boot_fingerprint": yielded.boot_fingerprint,
                    "disk_fingerprint": yielded.disk_fingerprint,
                    "boot_pid": yielded.boot_pid,
                    "sig": sig,
                    "streak": int(streak),
                    "first_tick": int(first_tick),
                    "last_tick": int(last_tick or (time.time() if now is None else now)),
                    "window_start": int(window_start or first_tick),
                    "status_record": status_path,
                    "board": board,
                    "filed_on": filed_on,
                    "escalation_ref": escalation_ref,
                },
            )
            if substrate:
                # The matrix rides the event, not just the body: a card's body is prose a reader
                # must parse, an event payload is what the sweeps and the audit read.
                _kb._append_event(
                    filed_on_conn, actor_id, SUBSTRATE_ADMISSION_HELD_EVENT,
                    {"matrix": yielded.matrix, "sig": sig, "held": []},
                )
        return actor_id
    except Exception as exc:
        _kb._log.warning(
            "kanban dispatcher: could not file the tree-skew actor (prefix=%s): %s", prefix, exc,
        )
        return None
    finally:
        if close_it:
            try:
                filed_on_conn.close()
            except Exception:
                pass


def _escalation_ledger_source() -> str:
    """Where the escalation registry module lives: ENV, then CONFIG, then the estate default.

    ``HERMES_KANBAN_ESCALATION_LEDGER`` wins, so an acceptance surface (or an operator) can point
    the door at a scratch ledger without touching the fleet's; ``kanban.escalation_ledger`` is the
    durable spelling; the estate default keeps the door armed on a tree whose config declares
    nothing. ``none``/``off`` DISABLES leg 1 and falls through to leg 2 -- a kill switch, never a
    silent no-op.
    """
    env = (os.environ.get(TICK_YIELD_ESCALATION_ENV) or "").strip()
    if env:
        return env
    try:
        from hermes_cli.config import load_config_readonly

        cfg = (load_config_readonly() or {}).get("kanban") or {}
    except Exception:
        cfg = {}
    if isinstance(cfg, dict):
        value = cfg.get(TICK_YIELD_ESCALATION_CONFIG_KEY)
        if value:
            return str(value)
    return TICK_YIELD_ESCALATION_DEFAULT


def _load_escalation_ledger():
    """``(module, None)`` when the registry loads, else ``(None, named-error)``.

    BY PATH, always: ``escalation_ledger.py`` lives in the platform estate
    (``/opt/hermes_prod/yaan-platform/scripts``), not on this package's import path, and pushing
    that directory onto ``sys.path`` from a dispatcher tick would let the estate's module names
    shadow this package's. A candidate that raises on import (a half-landed ledger) is a NAMED
    failure and leg 2 still runs -- no exception from here ever reaches the tick.
    """
    import importlib.util

    source = _escalation_ledger_source()
    if source.strip().lower() in TICK_YIELD_ESCALATION_DISABLED:
        return None, f"disabled by {TICK_YIELD_ESCALATION_ENV}={source!r}"
    path = Path(source).expanduser()
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_tick_yield_escalation_ledger", path)
        if spec is None or spec.loader is None:
            return None, f"no loadable module at {path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    return module, None


def _tree_skew_escalation_line(
    yielded: DispatcherTickYielded, *, episode: str, streak: int, status_path: str,
) -> str:
    """ONE deterministic line: the registry's ``reason``, the DM body, and the card's quote.

    No model, no prose to interpret: gate, imported label, disk label, pid, streak, episode and the
    status record's path -- the same facts the card carries, in one sentence a DM can be read from.
    """
    return (
        f"{TICK_YIELD_PREFIX} kanban dispatcher refusing to serve: imported tree content "
        f"{yielded.boot_label} (pid {yielded.boot_pid}) but the checkout is {yielded.disk_label}; "
        f"streak {streak} of {TICK_YIELD_ACTOR_THRESHOLD}; episode {episode}; "
        f"status record {status_path}"
    )


def _peer_run_tree_skew(line: str, episode: str) -> tuple[str, Optional[str]]:
    """Leg 2 of the door: ONE ``hermes -p default peer run`` nudge. ``(state, error)``, never raises.

    The verb is the ASYNC one (``peer run`` queues the turn and returns), and ``--idempotency-key``
    is the EPISODE identity, so a re-fire of the same window cannot nudge twice even if the record
    that was supposed to bound it was lost. ``escalation_ledger.notify()`` is the model for the env
    scrubbing: a tick fires inside another process's tree, so its board/profile pins (and a
    delegated child's context) must not leak into the child.
    """
    import subprocess

    hermes = os.environ.get("HERMES_ESCALATION_HERMES") or "hermes"
    cmd = [hermes, "-p", TICK_YIELD_ACTOR_ASSIGNEE, "peer", "run",
           f"yoyodine/{TICK_YIELD_ACTOR_ASSIGNEE}", "--idempotency-key", episode]
    env = dict(os.environ)
    for var in list(env):
        if var.startswith("HERMES_KANBAN_") or var == "HERMES_DELEGATED_CHILD_CONTEXT":
            env.pop(var, None)
    try:
        proc = subprocess.run(
            cmd, input=line, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=TICK_YIELD_ESCALATION_TIMEOUT_S, env=env,
        )
    except Exception as exc:
        return "failed", f"{exc.__class__.__name__}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:400] or f"rc={proc.returncode}"
        return "failed", detail
    return "sent", None


def _deliver_tree_skew_escalation(
    yielded: DispatcherTickYielded,
    *,
    episode: str,
    status_path: str,
    streak: int,
    record: Optional[dict] = None,
) -> dict:
    """The DOOR, attempted ONCE per episode: the registry row (+ its nudge), else the peer nudge.

    Returns ``{"at", "leg", "ref", "state", "error"}`` -- recorded on the status record so a later
    refusal of the same window can see the door was already knocked on, and a reader of the record
    can see WHICH leg carried it and what failed. Notes on the shape:

    * ``owner_lane`` is passed EXPLICITLY: ``resolver_for_board`` reads the catalogue DB and fails
      LOUD on an unreadable registry, and a delivery path that dies on a lookup is no door at all.
    * the escalation is NEVER closed from here -- closing carries the resolver's write-back
      (resolution, why-raisers-stuck, learning), which only the resolver can honestly write.
    * ``state`` is ``sent`` / ``failed`` / ``unavailable``; nothing here raises.
    """
    at = int(time.time())
    line = _tree_skew_escalation_line(
        yielded, episode=episode, streak=streak, status_path=status_path)
    module, error = _load_escalation_ledger()
    ref: Optional[str] = None
    if module is not None:
        try:
            row = module.register(
                source_kind=TICK_YIELD_ESCALATION_SOURCE_KIND,
                source_id=episode,
                board=TICK_YIELD_ACTOR_BOARD,
                owner_lane=TICK_YIELD_ACTOR_ASSIGNEE,
                reason=line,
                raised_by=TICK_YIELD_ESCALATION_RAISED_BY,
                issue_class=TICK_YIELD_ESCALATION_ISSUE_CLASS,
                submission=record or {"episode": episode},
            ) or {}
            ref = row.get("ref")
        except Exception as exc:
            error = f"register: {exc.__class__.__name__}: {exc}"
        if ref:
            try:
                module.notify(ref, TICK_YIELD_ACTOR_ASSIGNEE, line)
            except Exception as exc:
                # The ROW exists (the durable half landed); only the nudge failed. Say so, and do
                # not re-knock through leg 2: a second channel for one episode is noise.
                failed = f"notify: {exc.__class__.__name__}: {exc}"
                _kb._log.warning(
                    "kanban dispatcher: escalation %s raised but its nudge failed: %s", ref, failed)
                return {"at": at, "leg": "ledger", "ref": ref, "state": "failed",
                        "error": failed}
            return {"at": at, "leg": "ledger", "ref": ref, "state": "sent", "error": None}
    state, peer_error = _peer_run_tree_skew(line, episode)
    combined = "; ".join(part for part in (error, peer_error) if part) or None
    if state != "sent":
        _kb._log.warning(
            "kanban dispatcher: the tree-skew door delivered NOTHING for episode %s "
            "(leg 2 state=%s): %s", episode, state, combined,
        )
    return {
        "at": at,
        "leg": "peer",
        "ref": ref,
        "state": state,
        "error": combined,
    }





def _event_payload(raw) -> dict:
    """A ``task_events.payload`` blob as a dict (tolerant: a relic is ``{}``, never a raise)."""
    if isinstance(raw, dict):
        return raw
    try:
        doc = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else {}
    except (TypeError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


@dataclass(frozen=True)
class _PriorSkewEvent:
    """A prior guard card that PROVES a reload landed on THIS SAME imported content.

    The identity is ``boot_fingerprint`` -- the content a reload CHANGES (R-1).
    ``disk_fingerprint`` rides along as EVIDENCE ONLY: what a reload leaves behind is the checkout,
    so matching on it made an ordinary re-skew read as a broken door (card t_5a54ca29).
    """

    card_id: str
    boot_pid: int
    boot_fingerprint: str
    disk_fingerprint: str
    status: str
    completed_at: Optional[int]
    event_at: int
    boot_recorded_at: int


def _this_process_booted_at(boot_pid: int) -> Optional[int]:
    """When the process that imported this content recorded its boot, or ``None``.

    Read from the LIVE process first: the gate runs INSIDE the process it judges, so the in-memory
    record is the authoritative one. The persisted record is the fallback, accepted only when its
    stored identity still names the same process -- a recycled pid's record dates somebody else's
    boot, and dating the wrong boot is how a cleared episode would read as survival.

    ``None`` means the caller must NOT escalate (R-2): an undateable boot is not evidence that a
    prior card survived into it.
    """
    try:
        from hermes_cli import tree_fingerprint

        record = tree_fingerprint.boot_record() or {}
        if int(record.get("pid") or 0) == int(boot_pid) and record.get("recorded_at") is not None:
            return int(record["recorded_at"])
        persisted = tree_fingerprint.recorded_boot(int(boot_pid)) or {}
        if int(persisted.get("pid") or 0) != int(boot_pid):
            return None
        if persisted.get("recorded_at") is None:
            return None
        identity = persisted.get("identity")
        if identity and _process_fingerprint(int(boot_pid)) != identity:
            return None
        return int(persisted["recorded_at"])
    except Exception:  # health: allow BLE001 -- fail-open (t_5a54ca29): an unreadable boot record
        # means this process's boot cannot be dated, and an undateable boot must NOT escalate
        # (R-2) -- but it must never take the dispatch tick down either.
        return None


def _prior_filer_certified_dead(pid: int, boot_fingerprint: str) -> bool:
    """``pid`` filed the prior card and is PROVEN DEAD -- anchored to its OWN boot record.

    The liveness verdict is necessary but not sufficient (R-2, recycled-pid guard): a pid NUMBER is
    only evidence while it still means the same process. So the filer's persisted boot record has
    to exist, name that pid, and carry the same imported content the card claims; and where both
    sides are readable, the identity at that pid has to be the recorded one -- which is exactly
    what a recycled pid breaks, and an identity that now differs from the record is NOT a death.

    A missing or mismatched record is NOT certified: the card then describes a process this board
    can no longer corroborate, and an unreadable claim does not carry a design escalation.
    """
    try:
        from hermes_cli import tree_fingerprint

        record = tree_fingerprint.recorded_boot(int(pid)) or {}
    except Exception:  # health: allow BLE001 -- fail-open (t_5a54ca29): an unreadable record
        # cannot certify the filer, and an uncertified pid is simply not PROVEN DEAD (so it
        # cannot carry a design escalation) -- never a reason to take the tick down.
        return False
    if int(record.get("pid") or 0) != int(pid):
        return False
    if str(record.get("fingerprint") or "") != str(boot_fingerprint):
        return False
    recorded_identity = record.get("identity")
    if recorded_identity:
        live_identity = _process_fingerprint(int(pid))
        if live_identity is not None and live_identity != recorded_identity:
            return False  # the pid now names a DIFFERENT process: recycled, not dead
    return _pid_liveness(int(pid)) == WORKER_DEAD


def _skew_survived_a_reload(
    conn: sqlite3.Connection, yielded: DispatcherTickYielded, *, limit: int = 50,
) -> Optional[_PriorSkewEvent]:
    """The prior guard card PROVING a reload landed on THIS imported content and did not clear it.

    THE IDENTITY IS THE IMPORTED CONTENT, NEVER THE DISK (R-1). What a reload changes is the
    content a process imports; what a reload leaves behind is the checkout on disk. A prior card
    whose ``disk_fingerprint`` equals this tick's is therefore NOT evidence of survival -- it is
    the same skew measured by a DIFFERENT process, and 2026-10-21 is exactly that shape (b1b9cb6
    back to 47558cf inside five minutes): a re-skew onto a revision the fleet had already left.
    Matching on the disk made an ordinary re-entry read as a broken reload door.

    All three must hold; any unreadable field means NOT survived (fail closed):

      * R-1: the prior event imported the SAME content this process imported
        (``boot_fingerprint``) -- the only thing a reload can change;
      * it named a DIFFERENT process, and that process is PROVEN DEAD -- the liveness contract's
        verdict, never the negation of an unreadable probe: an unknown process is not evidence
        that a reload happened;
      * R-2: that card was not already CLEARED before this process booted. A prior card still open,
        or closed at/after this process's boot ``recorded_at``, is a reload that landed and came
        back -- which is a design question. One closed before that is a previous episode, already
        answered, and re-filing it as survival is how one card becomes 116.
    """
    boot_recorded_at = _this_process_booted_at(yielded.boot_pid)
    if boot_recorded_at is None:
        return None
    try:
        rows = conn.execute(
            "SELECT t.id AS id, t.status AS status, t.completed_at AS completed_at, "
            "e.payload AS payload FROM task_events e "
            "JOIN tasks t ON t.id = e.task_id WHERE e.kind = ? ORDER BY e.id DESC LIMIT ?",
            (TICK_YIELD_ACTOR_EVENT, int(limit)),
        ).fetchall()
    except Exception:  # pragma: no cover - a board without the event table has no history
        return None
    for row in rows:
        payload = _event_payload(row["payload"])
        if str(payload.get("boot_fingerprint") or "") != yielded.boot_fingerprint:
            continue
        try:
            pid = int(payload.get("boot_pid"))
        except (TypeError, ValueError):
            continue
        if pid == yielded.boot_pid:
            # The SAME frozen process: that repeat is the existing carrier's to absorb.
            continue
        boot_fingerprint = str(payload.get("boot_fingerprint") or "")
        if not _prior_filer_certified_dead(pid, boot_fingerprint):
            continue
        status = str(row["status"] or "")
        completed_at = row["completed_at"]
        if status in TICK_YIELD_ACTOR_ABSORB_SKIP_STATUSES:
            try:
                closed_at = int(completed_at)
            except (TypeError, ValueError):
                # Closed with no usable time: a previous episode and this one cannot be told
                # apart, and that is not proof the door failed (R-2, fail closed).
                continue
            if closed_at < boot_recorded_at:
                continue
        return _PriorSkewEvent(
            card_id=str(row["id"]),
            boot_pid=pid,
            boot_fingerprint=str(payload.get("boot_fingerprint") or ""),
            disk_fingerprint=str(payload.get("disk_fingerprint") or ""),
            status=status,
            completed_at=int(completed_at) if completed_at is not None else None,
            event_at=int(payload.get("last_tick") or payload.get("first_tick") or 0),
            boot_recorded_at=int(boot_recorded_at),
        )
    return None


def _tick_yield_escalation_body(
    yielded: DispatcherTickYielded,
    *,
    prior: _PriorSkewEvent,
    board: Optional[str],
    filed_on: Optional[str],
    status_path: str,
    streak: int,
    first_tick: int,
) -> str:
    """The escalation card's body: deterministic, MEASUREMENTS only, and the ONE question left.

    Every fact is labelled and quoted verbatim (R-4), each fingerprint printed beside the pid of
    the process that measured it, and the match the card rests on is the IMPORTED content. The
    body is built ONLY for the case R-1..R-3 established, so the question section stays: this card
    exists to get an answer, not to report a number.
    """
    import datetime

    def _iso(epoch):
        try:
            return datetime.datetime.fromtimestamp(int(epoch)).isoformat(timespec="seconds")
        except (TypeError, ValueError, OSError):
            return "?"

    closed_at = _iso(prior.completed_at) if prior.completed_at is not None else "(not closed)"
    return "\n".join([
        TICK_YIELD_ACTOR_DECLARATION,
        "",
        "## What was measured (verbatim, no conclusion)",
        "",
        f"- THIS process: pid {yielded.boot_pid}, imported content "
        f"`{yielded.boot_fingerprint}` (label `{yielded.boot_label}`), boot recorded at "
        f"{_iso(prior.boot_recorded_at)} (epoch {int(prior.boot_recorded_at)})",
        f"- the checkout on disk: `{yielded.disk_fingerprint}` (label `{yielded.disk_label}`)",
        f"- board refused: `{board or 'unknown'}`; card filed on: `{filed_on or board or 'unknown'}`",
        f"- consecutive refused ticks under THIS process: **{streak}**; episode "
        f"`{yielded.boot_label}:{int(first_tick)}` (first tick {_iso(first_tick)})",
        f"- non-green status record: `{status_path}`",
        "",
        "## The prior event this matched (SAME imported content)",
        "",
        f"- prior guard card: `{prior.card_id}` -- status `{prior.status}`, closed at {closed_at}",
        f"- filed by: pid {prior.boot_pid}, which imported content `{prior.boot_fingerprint}` "
        f"and measured disk content `{prior.disk_fingerprint}`",
        f"- that event was recorded at {_iso(prior.event_at)}, and that pid's liveness verdict is "
        "PROVEN DEAD",
        f"- this process booted at {_iso(prior.boot_recorded_at)}, so that card had not already "
        "cleared this skew before this process started",
        f"- match basis: the IMPORTED content `{yielded.boot_fingerprint}` is IDENTICAL in both. "
        "The disk fingerprints are quoted as evidence only and take NO part in the identity (R-1)",
        "",
        "## Why this is an escalation and not another guard card",
        "",
        f"`{prior.card_id}` was filed by pid {prior.boot_pid}, which is now PROVEN DEAD, and this",
        "process imported the SAME content: a reload DID land, the skew is back on the content it",
        "imported, and the prior card had not been cleared before this process booted. Repeating",
        "the reload demand would be a duplicate -- the fleet already measured 116 such cards",
        "(measured 2026-10-21: 116 `[guard] dispatcher is serving a mixed revision` rows across",
        "six boards), which is how a real stall becomes background noise.",
        "",
        "## The question this card exists to get answered",
        "",
        "The reload door does not clear the condition, so one of these is true and only a design",
        "decision can say which:",
        "",
        "1. the fingerprint is measuring something a reload cannot change (a path that is not the",
        "   imported one, a stale pyc the tree keeps regenerating, an interpreter loading a",
        "   DIFFERENT tree than the one fingerprinted), or",
        "2. the reload does not actually land on the disk bytes it reports (the launchd job",
        "   restarting onto a pinned tree, a second process serving the same port).",
        "",
        "Answer with the branch that holds and the card moves to the code lane that owns it. Until",
        "then NO further guard card is filed for this episode -- this one card carries it.",
    ])


def _file_tick_yield_escalation(
    conn: sqlite3.Connection,
    yielded: DispatcherTickYielded,
    *,
    prior: _PriorSkewEvent,
    board: Optional[str] = None,
    filed_on: Optional[str] = None,
    status_path: str = "",
    streak: int = 0,
    first_tick: int = 0,
    now: Optional[int] = None,
) -> Optional[str]:
    """File the ONE escalation card for a skew a reload did not clear (idempotent per EPISODE).

    The key is ``tree-skew-escalation:<imported content>:<first_tick>`` (R-3). One card per
    episode: a repeat refusal INSIDE the episode returns this same card, while an episode whose
    card has been closed mints a new key from its OWN ``first_tick`` instead of being swallowed by
    the old one -- the same window rule the guard card follows (DEC-1.4). Keying on the disk
    fingerprint instead made the escalation outlive its episode and cited a card closed months ago.
    """
    key = f"{TICK_YIELD_ESCALATION_KEY_PREFIX}{yielded.boot_fingerprint}:{int(first_tick)}"
    try:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status NOT IN ('done', 'archived') ORDER BY created_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        if existing is not None:
            return existing["id"]
        body = _tick_yield_escalation_body(
            yielded, prior=prior, board=board, filed_on=filed_on,
            status_path=status_path, streak=streak, first_tick=first_tick,
        )
        priority, ladder_note = _tick_yield_actor_priority(
            title=TICK_YIELD_ESCALATION_TITLE, body=body, board=filed_on or board or "",
            assignee=TICK_YIELD_ESCALATION_ASSIGNEE,
        )
        if ladder_note:
            body += f"\n\n_priority note: {ladder_note}_"
        escalation_id = _kb.create_task(
            conn,
            title=TICK_YIELD_ESCALATION_TITLE,
            body=body,
            assignee=TICK_YIELD_ESCALATION_ASSIGNEE,
            created_by="kanban-dispatcher",
            priority=priority,
            idempotency_key=key,
        )
        with _kb.write_txn(conn):
            _kb._append_event(
                conn, escalation_id, TICK_YIELD_ESCALATION_EVENT,
                {
                    "boot_fingerprint": yielded.boot_fingerprint,
                    "disk_fingerprint": yielded.disk_fingerprint,
                    "boot_pid": yielded.boot_pid,
                    "episode": f"{yielded.boot_label}:{int(first_tick)}",
                    "first_tick": int(first_tick),
                    "prior_card": prior.card_id,
                    "prior_boot_pid": prior.boot_pid,
                    "prior_boot_fingerprint": prior.boot_fingerprint,
                    "prior_disk_fingerprint": prior.disk_fingerprint,
                    "prior_status": prior.status,
                    "prior_completed_at": prior.completed_at,
                    "streak": int(streak),
                    "filed_on": filed_on,
                    "board": board,
                    "at": int(now if now is not None else time.time()),
                },
            )
        _kb._log.error(
            "kanban dispatcher: the tree skew SURVIVED a reload onto imported content %s "
            "(episode %s:%s); filed escalation %s to %s instead of another guard card",
            yielded.boot_label, yielded.boot_label, int(first_tick), escalation_id,
            TICK_YIELD_ESCALATION_ASSIGNEE,
        )
        return escalation_id
    except Exception as exc:
        _kb._log.warning(
            "kanban dispatcher: could not file the tree-skew ESCALATION (boot=%s, key=%s, "
            "prior=%s): %s",
            yielded.boot_fingerprint, key, prior.card_id, exc,
        )
        return None


def _tick_refused_for_tree_skew(
    conn: sqlite3.Connection,
    yielded: DispatcherTickYielded,
    *,
    board: Optional[str] = None,
    dry_run: bool = False,
) -> DispatchResult:
    """The whole non-green path for a refused tick: loud line, status record, and -- at the
    THRESHOLD, not before -- the DOOR (one delivery attempt per episode) and ONE acting card on the
    ops board. Returns the non-green result.

    Ordered on purpose: the door is knocked on BEFORE the card is written, so the card can quote the
    escalation ref it produced. Both are bounded to one attempt/one card per EPISODE -- the status
    record's ``delivery`` block is the bound for the door, the board's own absorb rule is the bound
    for the card.
    """
    status_path = "(dry run: nothing written)"
    if not dry_run:
        status_path, state = _record_tick_yield(yielded, board=board)
        streak = int(state.get("streak") or 0)
        if streak >= TICK_YIELD_ACTOR_THRESHOLD:
            sig = _yield_signature(yielded)
            first_tick = int(state.get("first_tick") or state.get("recorded_at") or time.time())
            episode = state.get("episode") or _episode_identity(sig, first_tick)
            if getattr(yielded, "kind", TICK_YIELD_KIND_TREE_SKEW) == SUBSTRATE_ADMISSION_KIND:
                # The substrate door is the DM PAIR on the boot-resident gateway (invariant I5),
                # NOT the escalation ledger: on 2026-10-26 every sanctioned raise path lived on the
                # denied volume, so a guard that raised through it was silent in the one episode
                # that needed it. One message per episode per target, bounded by the authority
                # record's own ``delivery`` block -- a fourth refusal cannot re-notify (I6).
                if getattr(yielded, "report", None) is not None:
                    # Carrier 1 of the ONE signal: the authority record is written on EVERY
                    # refused tick (the tick-streak sibling above is carrier 2, per-door
                    # corroboration). The streak is derived from the signature, so the two
                    # records agree in count and neither is the sole copy.
                    _sa.record(yielded.report, door="dispatcher")
                authority = _sa.read_status()
                if not (authority.get("delivery") or {}).get("at"):
                    delivery = _sa.deliver_once(authority, door="dispatcher")
                    _sa.merge_delivery(delivery)
                delivery = {}
            else:
                delivery = state.get("delivery") or {}
                if not delivery.get("at"):
                    delivery = _deliver_tree_skew_escalation(
                        yielded, episode=episode, status_path=status_path, streak=streak,
                        record=state,
                    )
                    _merge_tick_yield_delivery(delivery)
            _file_tick_yield_actor(
                conn, yielded, board=board, status_path=status_path, streak=streak,
                first_tick=first_tick,
                last_tick=int(state.get("last_tick") or 0),
                window_start=first_tick,
                escalation_ref=delivery.get("ref"),
            )
    logger.error("kanban dispatcher: %s (status record: %s)", yielded, status_path)
    return DispatchResult(tick_yielded=str(yielded))


def _raise_if_substrate_denied(*, report: Any = None, tier: str = "global") -> None:
    """DOOR 1 (design §3.2): a ``tier: global`` denial refuses the WHOLE dispatcher tick.

    Probe -> (record) -> refuse. The probe runs once per tick and its readings are cached for
    ``FRESH_WITHIN``, so two 60 s tickers cost ~N syscalls each, not 2N.

    * refused -> raise :class:`DispatcherTickYielded` with ``kind="substrate_denied"`` and the
      report attached (the caller writes the record and files the card);
    * denied but NOT refused (the kill switch is off) -> RECORD and admit: rollback step 1 turns
      the guard into a recorder, never into a second outage;
    * healthy -> write the RESTING-STATE heartbeat (ruling ``t_5411cc41`` §2 C1, card
      ``t_253a5e55``). A healthy tick MUST touch the record: without a writer ``ok`` is
      unreachable and the doctor row is a permanent yellow. The heartbeat bounds itself to one
      write per ``substrate_access.HEARTBEAT_EVERY`` and preserves a recovery receipt and a live
      episode, so the receipt the clearing tick wrote is never erased.

    I4 lives in ``substrate_access.refusal_for``: an ``indeterminate`` reading can never get here
    as a refusal.
    """
    probe = report if report is not None else _sa.probe_all(tier=tier)
    if _sa.refusal_for(probe, tier=tier):
        raise DispatcherTickYielded(
            _sa.signature(probe),
            probe.summary(),
            boot_pid=os.getpid(),
            kind=SUBSTRATE_ADMISSION_KIND,
            report=probe,
        )
    if probe.held_by_tier(tier):
        _sa.record(probe, door="dispatcher")
        return
    try:
        _sa.heartbeat(probe, door="dispatcher")
    except Exception:  # health: allow BLE001 -- the record is evidence, never the tick's work
        _kb._log.warning(
            "kanban dispatcher: could not write the substrate heartbeat", exc_info=True,
        )


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    host_budget_share: Optional[int] = None,
    lane_fair_spawn: bool = True,
    designated_pool_reserve: int = 1,
    default_max_runtime_seconds: Optional[int] = None,
) -> DispatchResult:
    """Run one dispatcher tick under the board's single-writer lock.

    Wraps :func:`_dispatch_once_locked` in the non-blocking :func:`_dispatch_tick_lock`
    so two dispatchers on one ``kanban.db`` never race a write tick on WAL
    frames. The loser returns an empty ``DispatchResult`` with
    ``skipped_locked=True`` and writes nothing; the lock is keyed on the
    resolved DB path so unrelated boards tick in parallel.

    CONSUMED FIRST (ruling ``t_8fed34c8``): a process whose imported tree no longer matches the
    checkout on disk refuses the whole tick -- no reclaim, no promotion, no spawn -- and returns a
    NON-GREEN ``DispatchResult``. This sits here rather than in the board loop because every entry
    point (the gateway's embedded dispatcher, ``run_daemon`` and the CLI ``dispatch`` verb) shares
    this function, so none of them is a hole. The check is once per tick and bounded
    (``hermes_cli.tree_fingerprint``: a stat walk on the steady state), never per card.
    """
    # Resolve the board this tick is actually against. The pin/name may be absent
    # (the CLI passes no board=), so read it off the open store — the same mapping
    # ``board_for_connection`` gives, never the ambient current board.
    resolved_board = board
    if not resolved_board:
        try:
            resolved_board = _kb.board_for_connection(conn)
        except Exception:
            resolved_board = None
    if resolved_board and not _kb.board_dispatch_enabled(resolved_board):
        result = DispatchResult(skipped_board_disabled=True)
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
        return result

    try:
        _raise_if_tree_skewed()
        _raise_if_substrate_denied()
    except DispatcherTickYielded as yielded:
        result = _tick_refused_for_tree_skew(conn, yielded, board=board, dry_run=dry_run)
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
        return result

    def _locked_tick() -> DispatchResult:
        return _dispatch_once_locked(
            conn,
            spawn_fn=spawn_fn,
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
            max_spawn=max_spawn,
            max_in_progress=max_in_progress,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            board=board,
            default_assignee=default_assignee,
            max_in_progress_per_profile=max_in_progress_per_profile,
            reconcile_orphans=reconcile_orphans,
            host_budget_share=host_budget_share,
            lane_fair_spawn=lane_fair_spawn,
            designated_pool_reserve=designated_pool_reserve,
            default_max_runtime_seconds=default_max_runtime_seconds,
        )

    try:
        db_path = _kb.kanban_db_path(board=board)
    except Exception:
        # Must not lose the tick — fall through to an unguarded dispatch.
        result = _locked_tick()
        _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
        return result
    with _kbc._dispatch_tick_lock(db_path) as held:
        if not held:
            result = DispatchResult(skipped_locked=True)
        else:
            # DEC-3 + card t_028ad743: retire the guards whose episode is OVER, BEFORE this tick
            # claims or spawns anything. THE PROOF IS THE PLACEMENT -- a tick that reached here
            # passed ``_raise_if_tree_skewed`` (the tree this process imported IS the checkout on
            # disk) and holds the board's dispatch lock. It must run before the claim phase because
            # the tick's own reconcile otherwise releases and CLAIMS a fallback-filed guard: the
            # card came back ``running``, the kernel refused the close, and the duplicate was
            # re-dispatched to its lane on every tick while never being retired.
            #
            # Both doors keep their own fault isolation: neither may take a tick down.
            if not dry_run:
                _retire_cleared_tree_skew_cards(conn, board=resolved_board)
                # DOOR 3's retirement (card t_0ae49984): the substrate episode's guard, same
                # placement proof and the same fault isolation -- a tick that reached here passed
                # the substrate door, so the episode is over.
                _retire_cleared_substrate_cards(conn, board=resolved_board)
            result = _locked_tick()
            # Still under the dispatch lock: periodic PASSIVE WAL checkpoint.
            _kbc._maybe_checkpoint_wal(conn, db_path)
    # Lock released. Fire the tick observer strictly OUTSIDE the critical
    # section: a slow subscriber must never stall a sibling dispatcher's tick.
    _kb._fire_dispatch_tick_hook(result, board=board, dry_run=dry_run)
    return result


def _call_spawn_fn(spawn_fn, task: Task, workspace: str, board: Optional[str]) -> Optional[int]:
    """Back-compat: older spawn_fn signatures (and test stubs) accept only
    ``(task, workspace)``; pass ``board`` only when the callable supports it."""
    import inspect
    try:
        sig = inspect.signature(spawn_fn)
        if "board" in sig.parameters:
            return spawn_fn(task, workspace, board=board)
        return spawn_fn(task, workspace)
    except (TypeError, ValueError):
        return spawn_fn(task, workspace)


def _self_review_reason(
    conn: sqlite3.Connection, task_id: str, assignee: str,
) -> Optional[str]:
    """Why ``assignee`` must not be spawned to review ``task_id``, else ``None``.

    ``"assignee_is_implementer"`` — the card's recorded implementer IS this
    assignee (canonicalised the same way ``request_review`` canonicalises a
    reviewer, so capitalization/case-only differences cannot slip past).
    ``"implementer_unknown"`` — the card carries no ``review_requested``
    implementer provenance at all, so a distinct reviewer cannot be PROVEN.
    Fail-closed: unknown provenance parks rather than trusts, since the default
    ``reviewer=None`` handoff records no reviewer and leaves the author as the
    assignee.
    """
    implementer = _kb.review_implementer(conn, task_id)
    if not implementer:
        return "implementer_unknown"
    if _kb._canonical_assignee(implementer) == _kb._canonical_assignee(assignee):
        return "assignee_is_implementer"
    return None



def _dispatch_lane_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    assignee: str,
    result: DispatchResult,
    *,
    lane: str,
    dry_run: bool,
    ttl_seconds: Optional[int],
    board: Optional[str],
    failure_limit: int,
    spawn_fn,
    per_profile_cap: Optional[int],
    per_profile_running: dict[str, int],
    default_max_runtime_seconds: int = 0,
) -> bool:
    """Guard, claim, resolve the workspace and spawn one ready/review row.
    Returns True when a spawn slot was consumed (real or ``dry_run``); every
    skip is recorded on ``result``.
    """
    task_id = row["id"]
    # Non-profile assignees (control-plane lanes that pull via ``claim_task``)
    # would fail ``hermes -p <assignee>`` at startup and loop ready→crash→ready
    # forever. Bucketed apart from skipped_unassigned: the operator cannot fix
    # it by assigning a profile, and health telemetry suppresses "stuck" for it.
    profile_exists = _profile_exists_fn()
    if profile_exists is not None and not profile_exists(assignee):
        result.skipped_nonspawnable.append(task_id)
        # Per-task diagnostic so ``show``/``tail`` name the missing profile instead of leaving
        # the card in ``ready`` with zero board evidence (#122422). Unlike a respawn guard the
        # condition never expires on its own, so write it once: a repeat only when something
        # else happened on the card since (reassign, comment) — not one row per tick forever,
        # and not one row per foreign home per tick on a shared board (#101015).
        if not dry_run:
            with _kb.write_txn(conn):
                last = conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,)).fetchone()
                if (last is None or last["kind"] != "skipped_nonspawnable"
                        or last["payload"] != _kb._json_or_null({"assignee": assignee})):
                    _kb._append_event(conn, task_id, "skipped_nonspawnable", {"assignee": assignee})
        return False
    # Author exclusion: the review lane must never spawn a task's own
    # implementer with the review skill — the bot that wrote the change would be
    # grading (and could land) its own work. Fail-closed: park the row until a
    # distinct reviewer is named. Placed before the cap / respawn guards so
    # ``dry_run`` sees exactly the same decision. Deliberately no task event:
    # this guard can stay engaged for many ticks and an event per tick would
    # spam ``hermes kanban tail``.
    if lane == "review":
        self_review = _self_review_reason(conn, task_id, assignee)
        if self_review is not None:
            result.skipped_self_review.append((task_id, self_review))
            return False
    # Per-profile cap: one profile's local model / API quota / browser pool
    # must not be overwhelmed by a fan-out even with global headroom.
    if per_profile_cap is not None:
        current = per_profile_running.get(assignee, 0)
        if current >= per_profile_cap:
            result.skipped_per_profile_capped.append((task_id, assignee, current))
            return False
    guard_reason = check_respawn_guard(conn, task_id, lane=lane)
    if guard_reason is not None:
        result.respawn_guarded.append((task_id, guard_reason))
        # Event so ``hermes kanban tail`` shows why the task looks stuck.
        # Honour kanban.default_assignee: when the dispatcher hits an unassigned ready task and an
        # operator-configured fallback exists, persist the assignment and proceed. This removes the
        # dashboard footgun where a task created without an assignee parks in 'ready' forever even though
        # the operator's intent ("default") was perfectly clear (#27145). Mutating the row (not just the
        # in-memory view) keeps diagnostics and the board state consistent: the task is now legitimately
        # owned by ``kanban.default_assignee``, not "unassigned but secretly routed".
        if not dry_run:
            with _kb.write_txn(conn):
                _kb._append_event(conn, task_id, "respawn_guarded", {"reason": guard_reason})
        return False

    # TOP-LEVEL-ONLY gate (RUN door, ruling t_efa908e8 / card t_0d8f71ae): a card
    # that declares it needs a genuine top-level context is NEVER spawned -- a
    # dispatched worker is structurally fenced from the work (delegate-child
    # fence + board confinement). The card is PARKED ``scheduled`` (due_at NULL)
    # with ONE deduped event + ONE comment naming the routes, and the hold is
    # counted on the tick's suppression line (a silent hold is a defect).
    # Re-checked LIVE, so an amendment that adds the marker after filing is
    # caught. Respects ``dry_run``: reports the hold, writes nothing.
    if _hold_top_level_only(conn, task_id, dry_run=dry_run, board=board):
        result.skipped_top_level_only.append(task_id)
        return False

    # SUBSTRATE ADMISSION -- door 3 (card t_0ae49984, design t_72e9bf03): a card whose own
    # workspace root is on a denied substrate is HELD -- NOT claimed, NOT failed, NOT blocked --
    # and this tick carries on with its siblings (invariant I2). Deliberately BEFORE the claim:
    # claim-then-refuse would write a claim and charge the card a failure it did not earn, and it
    # would mint a workspace on a volume nobody can read. The card stays `ready`, so the next tick
    # re-probes and admits it the moment the substrate is readable again.
    if _hold_substrate_denied(conn, task_id, dry_run=dry_run, board=board):
        result.substrate_held.append(task_id)
        return False

    def _count_spawn(name: str) -> None:
        # Later rows in this tick respect the per-profile cap; subsequent
        # ticks re-query from the DB.
        if per_profile_cap is not None and name:
            per_profile_running[name] = per_profile_running.get(name, 0) + 1

    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        _count_spawn(assignee)
        return True
    claim = _kb.claim_review_task if lane == "review" else _kb.claim_task
    claimed = claim(conn, task_id, ttl_seconds=ttl_seconds)
    if claimed is None:
        return False
    # SLOT RECYCLING (ruling t_b2865b89 §4). A card that carried no ``max_runtime_seconds`` is
    # stamped with the dispatcher default HERE — at claim time — so ``enforce_max_runtime`` can
    # always reap the slot instead of leaving the tick budget at 0 for hours.
    #
    # THE BOUNDARY (do not remove this pin): the DEFAULTED cap must never reach
    # ``_worker_terminal_timeout_env``. That call raises the child worker's ``TERMINAL_TIMEOUT`` to
    # ``cap - 30 s``; letting a scheduling default through would jump every lane in the fleet from
    # the generic terminal default to 7170 s, a behaviour change hidden inside a scheduling fix. So
    # the card's EXPLICIT pre-stamp value is captured first and pinned back onto the Task handed to
    # the spawn — the DB column gets the default, the spawned worker sees ONLY what the author set.
    explicit_max_runtime_seconds = claimed.max_runtime_seconds
    if default_max_runtime_seconds:
        _stamp_default_max_runtime_seconds(conn, task_id, limit=default_max_runtime_seconds)
    claimed.max_runtime_seconds = explicit_max_runtime_seconds
    try:
        resolved_branch_name = None
        if claimed.workspace_kind == "worktree":
            workspace, resolved_branch_name = _kbw._resolve_worktree_workspace(claimed, board=board)
        else:
            workspace = _kbw.resolve_workspace(claimed, board=board)
    except Exception as exc:
        result.spawn_failed.append((claimed.id, claimed.assignee or "", f"workspace: {exc}"))
        if _record_spawn_failure(
            conn, claimed.id, exc, failure_limit=failure_limit, board=board, prefix="workspace: ",
        ):
            result.auto_blocked.append(claimed.id)
        return False
    _kbw.set_workspace_path(conn, claimed.id, str(workspace))
    if claimed.workspace_kind == "worktree":
        _kbw.set_branch_name(conn, claimed.id, resolved_branch_name or (claimed.branch_name or "").strip() or f"wt/{claimed.id}")
    _kbw._maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
    if lane == "review":
        # Force-load sdlc-review; the kanban lifecycle is already in every
        # worker's system prompt via KANBAN_GUIDANCE.
        claimed.skills = list(dict.fromkeys([*(claimed.skills or []), "sdlc-review"]))
    try:
        pid = _call_spawn_fn(spawn_fn if spawn_fn is not None else _default_spawn, claimed, str(workspace), board)
        if pid:
            _set_worker_pid(conn, claimed.id, int(pid))
        # Fires AFTER the PID (when reported) is durably persisted. Best-effort.
        _kb._fire_worker_spawned_hook(conn, claimed, str(workspace), pid, board=board)
        # consecutive_failures is deliberately NOT reset here: resetting on
        # spawn would let a task that keeps timing out loop forever. Cleared
        # only on successful completion (complete_task).
        result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
        _count_spawn(claimed.assignee)
        return True
    except Exception as exc:
        result.spawn_failed.append((claimed.id, claimed.assignee or "", str(exc)))
        # ONE classifier decides the owner of this failure (D5): the host refusing to place
        # the worker, THIS tree's own modules disagreeing, or the card. Only the last one
        # spends the card's retry budget.
        if _record_spawn_failure(
            conn, claimed.id, exc, failure_limit=failure_limit, board=board,
        ):
            result.auto_blocked.append(claimed.id)
        return False


def _hold_substrate_denied(
    conn: sqlite3.Connection, task_id: str, *, dry_run: bool = False,
    board: Optional[str] = None,
) -> bool:
    """DOOR 3 (design §3.4, card ``t_0ae49984``): is THIS card's own workspace root readable?

    Returns True when the card must be HELD: its resolved workspace root answered a DENIAL to a
    real operation. The card is NOT claimed, NOT failed, NOT blocked and NOT failure-counted
    (invariant I1) -- it stays ``ready``, so the next tick re-probes and admits it the moment the
    substrate is readable. Siblings are unaffected (invariant I2): this is asked per card, and a
    False answer lets that card through unchanged.

    WHY THE ROOT, NOT THE WORKSPACE: resolving a worktree workspace MINTS it, and minting one on a
    denied volume is exactly what this gate exists to prevent. ``planned_workspace_root`` computes
    where the workspace would land without creating anything, and the probe runs on the nearest
    existing ancestor -- which, during the measured fault, IS the denied path itself, because
    ``stat``/``exists``/``isdir`` answered TRUE throughout it (F1).

    NOT SILENT: ONE deduped ``substrate_admission_held`` event plus ONE comment per hold episode,
    the same shape as the top-level-only door -- the guard runs every tick, and a row per tick
    would bury the board with the noise that makes a starved board read as an idle one. ``dry_run``
    reports the hold and writes nothing. A store fault never takes the tick down.
    """
    try:
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
    except Exception:
        _kb._log.debug("kanban dispatch: substrate hold could not read %s", task_id, exc_info=True)
        return False
    if row is None:
        return False
    try:
        root = _kbw.planned_workspace_root(
            {"workspace_kind": row["workspace_kind"], "workspace_path": row["workspace_path"]},
            board=board,
        )
    except Exception:
        _kb._log.debug("kanban dispatch: no planned root for %s", task_id, exc_info=True)
        return False
    if root is None:
        return False
    probe = _sa.unit_substrate([root], tier="unit")
    denied = _sa.refusal_for(probe, tier="unit")
    if not denied:
        return False
    if dry_run:
        return True
    # Count the unit on the AUTHORITY record (design §3.4: "counted in the record as held").
    # `yielded=False` is the honest state: the global tier is green and only THIS unit is held, so
    # the record must not read as "the host is refusing to spawn" (that state owns the guard card
    # and the retirement sweep).
    try:
        _sa.record(
            probe, door="dispatcher", yielded=False,
            held=[{
                "task": task_id,
                "root": str(root),
                "errno": denied[0].errno if denied else None,
                "op": denied[0].op if denied else None,
            }],
        )
    except Exception:  # health: allow BLE001 -- evidence, never the hold itself
        _kb._log.warning(
            "kanban dispatch: could not record the substrate hold for %s", task_id, exc_info=True,
        )
    payload = {
        "reason": "substrate_denied",
        "root": str(root),
        "sig": _sa.signature(probe),
        "matrix": probe.matrix(),
        "board": board,
    }
    try:
        with _kb.write_txn(conn):
            last = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                f"AND kind = '{SUBSTRATE_ADMISSION_HELD_EVENT}' "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,),
            ).fetchone()
            if last is None or last["payload"] != _kb._json_or_null(payload):
                _kb._append_event(conn, task_id, SUBSTRATE_ADMISSION_HELD_EVENT, payload)
                _kb.add_comment(
                    conn, task_id, "dispatcher",
                    f"**Held by substrate admission** -- {SUBSTRATE_ADMISSION_PREFIX}\n\n"
                    f"This card was NOT claimed, failed or blocked: its own workspace root "
                    f"`{root}` answered a denial to a real operation "
                    f"({_sa._matrix_sentence(probe.matrix())}), so a worker started on it now "
                    f"would be born into a substrate nobody can read.\n\n"
                    f"It stays `ready` and the next dispatcher tick re-probes; nothing here needs "
                    f"a decision or a card-level retry. The evidence record is "
                    f"`{_sa.status_path()}` (boot volume). Note that `stat`/`exists` answer OK "
                    f"during this fault -- only the operation itself is the signal.",
                )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: substrate hold could not be recorded for %s", task_id, exc_info=True,
        )
    return True


def _hold_top_level_only(
    conn: sqlite3.Connection, task_id: str, *, dry_run: bool = False,
    board: Optional[str] = None,
) -> bool:
    """The RUN door of the top-level-only gate (hermes_cli/kanban_top_level_gate.py).

    Returns True when the card declares ``GATE-TOP-LEVEL-ONLY`` and must not be
    spawned, else False. Deterministic: one regex pass over the card's own
    LIVE title/body (re-read, exactly as :func:`check_consent_guard` re-reads, so
    an amendment that adds the marker after filing is honoured -- the create door
    catches the marker at birth, this door catches it later).

    The hold is never silent. The card is PARKED ``scheduled`` with ``due_at``
    NULL (waiting on a human, not on time; ``recompute_ready`` promotes only
    ``todo``/``blocked``, so it stays non-dispatchable), with ONE deduped
    ``top_level_only_held`` event and ONE comment naming the sanctioned top-level
    routes. The dedupe keeps one row per hold episode -- the guard runs every
    tick, and a row per tick would bury ``hermes kanban tail`` with the very
    noise that makes a starved board read as an idle one. A write fault must
    never take the tick down: the next tick retries, and the card is not spawned
    either way. ``dry_run`` reports the hold and writes nothing.
    """
    from hermes_cli import kanban_top_level_gate as _tlg

    row = conn.execute(
        "SELECT title, body, status FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None or row["status"] not in ("ready", "review"):
        return False
    if not _tlg.declares(row["title"], row["body"]):
        return False
    if dry_run:
        return True
    payload = {
        "reason": "declared",
        "marker": _tlg.SENTINEL,
        "board": board,
        "routes": list(_tlg.ROUTES),
    }
    try:
        with _kb.write_txn(conn):
            last = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'top_level_only_held' "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,),
            ).fetchone()
            if last is None or last["payload"] != _kb._json_or_null(payload):
                _kb._append_event(conn, task_id, "top_level_only_held", payload)
                _kb.add_comment(conn, task_id, "dispatcher", _tlg.park_comment(task_id))
    except Exception:
        _kb._log.debug(
            "kanban dispatch: top-level-only hold could not be recorded for %s",
            task_id, exc_info=True,
        )
    try:
        _kb.schedule_task(
            conn, task_id, due_at=None,
            reason=f"top-level-only: {_tlg.ROUTES[0]}",
        )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: top-level-only park failed for %s", task_id, exc_info=True,
        )
    return True


def _park_for_consent(conn: sqlite3.Connection, task_id: str, verdict) -> None:
    """Record the refusal on the card, then hold it (``needs_input`` is sticky).

    The event dedupe keeps ONE comment per distinct cause: the guard runs every
    tick, and a comment per tick would bury the board. A store outage, or a
    failed write, must never take the dispatcher down with it -- the next tick
    retries, and the card is not spawned either way.
    """
    from hermes_cli import kanban_consent_gate as _cg

    payload = {
        "cause": verdict.cause,
        "trigger": verdict.trigger,
        "refs": [r.ref for r in verdict.refs],
    }
    try:
        with _kb.write_txn(conn):
            last = conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'consent_refused' "
                "ORDER BY created_at DESC, id DESC LIMIT 1", (task_id,),
            ).fetchone()
            if last is None or last["payload"] != _kb._json_or_null(payload):
                _kb._append_event(conn, task_id, "consent_refused", payload)
                _kb.add_comment(
                    conn, task_id, "dispatcher",
                    "CONSENT REFUSED before the run (not spawned).\n\n"
                    + _cg.refusal_message(task_id, verdict),
                )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: consent refusal could not be recorded for %s", task_id, exc_info=True,
        )
    try:
        _kb.block_task(conn, task_id, reason=f"consent: {verdict.cause}", kind="needs_input")
    except Exception:
        _kb._log.debug(
            "kanban dispatch: consent refusal could not park %s", task_id, exc_info=True,
        )


def _apply_default_assignee(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, dry_run: bool,
) -> bool:
    """Persist ``kanban.default_assignee`` on an unassigned ready row.

    Mutating the row keeps board state honest: the task is legitimately owned
    by the default, not "unassigned but secretly routed". ``dry_run`` reports
    without writing. Returns False when the write failed.

    A row whose stored skills no worker for the default can load is refused
    here as well: the assign would hand the dispatcher a card that crash-loops
    on ``Unknown skill(s)`` and auto-blocks with the reason visible only in a
    worker log. ``kanban.default_assignee`` is a config value, so this attach is
    the one with no operator in the loop to read a refusal — it is logged.
    """
    row = conn.execute("SELECT skills FROM tasks WHERE id = ?", (task_id,)).fetchone()
    try:
        _kb._refuse_unloadable_skills(row["skills"] if row is not None else None, assignee)
    except ValueError as exc:
        _kb._log.warning(
            "kanban dispatch: not applying default_assignee=%r to task %s: %s",
            assignee, task_id, exc,
        )
        return False
    if dry_run:
        return True
    try:
        with _kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ? "
                "AND (assignee IS NULL OR assignee = '')",
                (assignee, task_id),
            )
            _kb._append_event(
                conn, task_id, "assigned",
                {"assignee": assignee, "source": "kanban.default_assignee"},
            )
    except Exception:
        _kb._log.debug(
            "kanban dispatch: failed to apply default_assignee=%r to task %s",
            assignee, task_id, exc_info=True,
        )
        return False
    return True


def _check_identity_generation(conn: sqlite3.Connection, board: Optional[str] = None) -> list[str]:
    """R6: stamp the deployed identity generation and alert on a one-tick divergence.

    The shape this dispatcher WRITES is stamped on disk on the first tick and compared on every
    later tick, so a tree write that drops the witness bytes or an interpreter change that moves the
    shape is detected in ONE tick and alerted -- instead of being discovered by a false crash sweep
    hours later (the recurring class: t_1e6a58c3, 2026-09-26, 2026-09-29, t_ae80d242, 2026-10-10).

    Best-effort by design: a guard that cannot read its own record, or a board that refuses the
    acting card, must never take the dispatcher tick down. Returns the divergence reasons (empty
    when the generation is unchanged), so ``DispatchResult`` carries the alert as well as the log.
    """
    try:
        tick = _crash_guard.check_deployed_generation(
            sample=_process_fingerprint(os.getpid()),
            tree=_crash_guard.dispatching_tree(),
            witness_symbols=_crash_guard.witness_symbols_present(sys.modules[__name__]),
        )
    except Exception:
        _kb._log.debug("kanban dispatch: identity-generation check failed", exc_info=True)
        return []
    if not tick.alerting:
        return []
    try:
        _crash_guard.alert_generation_divergence(
            conn, tick, board=board, tree=_crash_guard.dispatching_tree(),
        )
    except Exception:
        _kb._log.debug("kanban dispatch: identity-generation alert failed", exc_info=True)
    return list(tick.reasons)


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
    board: Optional[str] = None,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    reap_worker_zombies()
    result.reaped_terminal_workers = reap_terminal_workers(conn)
    result.reclaimed = _kb.release_stale_claims(conn, failure_limit=failure_limit)
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    result.stale = detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    # R6 BEFORE the sweep: the deployed identity generation is stamped and compared first, so a
    # regression is on record in ONE tick instead of being discovered by a false crash sweep later.
    result.identity_generation_alerts = _check_identity_generation(conn, board=board)
    result.crashed = detect_crashed_workers(conn, board=board)
    _refused = getattr(detect_crashed_workers, "_last_crash_sweep_abort", None)
    if _refused is not None:
        result.crash_sweep_refused = {
            "signature": _refused.signature,
            "count": _refused.count,
            "threshold": _refused.threshold,
            "ended_at": _refused.ended_at,
            "task_ids": list(_refused.task_ids),
        }
    result.crash_sweep_held = list(getattr(detect_crashed_workers, "_last_crash_sweep_held", []) or [])
    # Side-channel attributes (see detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears;
    # storage-locked tasks went back to ``ready`` the same way and the guard spaces
    # them on their own cooldown.
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.storage_locked.extend(getattr(detect_crashed_workers, "_last_storage_locked", []))
    result.timed_out = enforce_max_runtime(conn)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)
    # Door 3 of the above-tranche guard (t_6ce41549, restored by card t_2e107499): a row ABOVE
    # the reserved tranche is LOWERED into its class ceiling, board-locally, on every tick.
    # Deterministic and idempotent, and placed after promotion because a promoted row is exactly
    # the kind of row a later door could have written above the ceiling: the invariant is
    # restored before the spawn decision is taken, never after it.
    result.priority_demoted = []
    result.priority_demote_refused = []
    try:
        demoted = _kb.demote_above_tranche(conn, board=board)
    except Exception as exc:  # a repair must never stop the board dispatching
        # THE PASS CANNOT TAKE THE TICK DOWN (measured 2026-10-01: an abort in this pass — door 3
        # refusing the pass's own write — killed every `defcon` tick for 6.5 h, and the tick died
        # BEFORE the spawn loop, so 452 ready cards spawned nothing). Whatever the guard and the
        # doors disagree about, the repair of an already-broken invariant is not what stops the
        # board's dispatching: it is recorded on the tick's own result and logged at ERROR.
        demoted = []
        result.priority_demote_error = f"{type(exc).__name__}: {exc}"
        _kb._log.error(
            "kanban: the above-tranche repair pass FAILED on board %s; the tick continues and "
            "dispatches from a board still carrying an over-claim: %s",
            board or "-", result.priority_demote_error,
        )
    result.priority_demoted = [
        (row["task_id"], row["was"], row["now"])
        for row in demoted if row.get("now") is not None
    ]
    result.priority_demote_refused = [
        f"{row['task_id']} ({row['was']} -> {row['now']}): {row.get('error')}"
        for row in demoted if row.get("error")
    ]


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
    host_budget_share: Optional[int] = None,
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. It is the board's OWN ceiling: ``kanban.max_spawn_by_board``
    when the board is named there, else ``kanban.max_spawn``.
    ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    ``host_budget_share`` is this board's slice of the FREE host slots for this
    tick, allocated across the boards by the gateway dispatcher
    (``gateway.kanban_watchers_dispatcher.host_budget_shares_by_ceiling``): the
    host cap still bounds the fleet, and the share stops one board's queue from
    taking the slots every other board is waiting for. ``None`` = no share was
    allocated, which is every single-board caller (the CLI daemon and
    ``hermes kanban dispatch``).
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker makes a terminal
    # board call (kanban_complete/kanban_block/kanban_request_review) or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            # The board is at its OWN ceiling (its per-board value, or the
            # global max_spawn). Nothing starts until it drains, and from the
            # outside the queue looks idle — name what the ceiling held back so
            # the tick is reportable, never a silent no-op. Also name the cap on
            # the result: health telemetry must read this as "deferred, capacity
            # full" and stay quiet, not as "nothing spawned, something is broken".
            result.spawn_budget_blocked = "max_spawn"
            result.deferred_board_capped = spawnable_pending_ids(conn)
            result.spawn_budget_detail = {
                "blocked_by": "max_spawn",
                "cap": int(max_spawn),
                "board_running": int(running_count),
                "deferred": len(result.deferred_board_capped),
            }
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            # The HOST-wide cap held the tick. Its held cards are named too: a
            # gate that declines every card without naming one is the SEV1 of
            # card t_843cb0a9 -- the operator surface reported every skip bucket
            # empty while the host sat at 12 running against a cap of 8.
            result.spawn_budget_blocked = "max_in_progress"
            result.deferred_in_progress_capped = spawnable_pending_ids(conn)
            result.spawn_budget_detail = {
                "blocked_by": "max_in_progress",
                "cap": int(max_in_progress),
                "board_running": int(running_count),
                "host_running": int(total_running),
                "host_cap": int(max_in_progress),
                "deferred": len(result.deferred_in_progress_capped),
            }
            return False, None
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # This board's SHARE of the host budget, handed out by the gateway
    # dispatcher. 0 means more boards had startable work than there were free
    # slots, so this board waits a tick: a rotation, not a fault, and recorded
    # the same way so a board that keeps losing the race is still visible.
    if host_budget_share is not None:
        share = max(int(host_budget_share), 0)
        if share <= 0:
            result.deferred_host_capped = spawnable_pending_ids(conn)
            return False, None
        if spawn_budget is None or spawn_budget > share:
            spawn_budget = share

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def zero_run_ready(
    conn: sqlite3.Connection, *, now: Optional[float] = None,
    min_age_seconds: Optional[float] = None,
) -> dict:
    """Stuck-ready probe — see :func:`hermes_cli.kanban_db.zero_run_ready`.

    Defined ONCE in ``kanban_db`` so every dispatcher health surface (the
    gateway watcher, the standalone daemon, the board-stats / ops-API export)
    reads the same query; this module only forwards, and is the board-aware
    entry point plugin consumers reach through ``kanban_db``.
    """
    if min_age_seconds is None:
        return _kb.zero_run_ready(conn, now=now)
    return _kb.zero_run_ready(conn, now=now, min_age_seconds=min_age_seconds)


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order.

    Ordering is the shared age-aware effective priority
    (``kanban_db.effective_priority_order_sql``): a card that has waited long
    enough outranks fresher higher-priority work, so a low-priority lane can
    never be starved indefinitely by a steady trickle of P0/P1 cards. Both lane
    orders (ready and review) call the one helper so they cannot drift.
    """
    order_body, order_params = _kb.effective_priority_order_sql()
    return conn.execute(
        "SELECT id, assignee, priority FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        f"ORDER BY {order_body}",
        order_params,
    ).fetchall()


def _spawnable_lane_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Ready (and, when enabled, review) rows a worker could start, head first.

    "Spawnable" is the dispatcher's own gate as far as a read-only pass can
    apply it: review rows only when review dispatch is on, and never a card
    without an assignee or assigned to a profile this host cannot spawn — those
    wait for ROUTING, not for a free slot.
    """
    rows = _lane_rows(conn, "ready")
    if review_dispatch_enabled():
        rows = rows + _lane_rows(conn, "review")
    profile_exists = _profile_exists_fn()
    if profile_exists is None:
        return [row for row in rows if row["assignee"]]
    return [row for row in rows if row["assignee"] and profile_exists(row["assignee"])]


def spawnable_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """This board's spawnable cards (``id``/``assignee``/``priority``), head first.

    The gateway dispatcher allocates the host budget by ranking EVERY card on
    EVERY board (``gateway.kanban_watchers_dispatcher.host_budget_shares_by_priority``),
    so it needs the board's own dispatch-order rows — the same order
    :func:`_spawnable_lane_rows` produces. Read-only.
    """
    return _spawnable_lane_rows(conn)


def head_of_line_priority(conn: sqlite3.Connection) -> Optional[int]:
    """Priority of the card this board would spawn next, or ``None``.

    ``_lane_rows`` orders a lane by the shared effective priority, so the head
    card's own ``priority`` is the priority of the work this board would start
    next — ranking boards by it ranks them by exactly the order their own cards
    will be spawned in.

    ``None`` means nothing on this board could be started by a worker. A caller
    allocating a SHARED budget must still visit such a board (reclaim,
    promotion, decomposition and health work is board-local); it just cannot
    rank it.
    """
    rows = _spawnable_lane_rows(conn)
    if not rows:
        return None
    return int(rows[0]["priority"])


def total_running_all_boards() -> Optional[int]:
    """Running tasks across EVERY board — the host's whole occupancy.

    This is the number a caller subtracts from the host cap to find the free
    slots, so a partial answer is worse than none: ``None`` means at least one
    board could not be read and the caller must fall back to its previous
    behaviour instead of handing out slots it cannot prove are free. Boards are
    matched by resolved DB path, so ``HERMES_KANBAN_DB`` (every board pinned to
    one file) is counted once, exactly like ``count_running_tasks_other_boards``.
    """
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return None
    seen: set[str] = set()
    total = 0
    for meta in boards:
        slug = meta.get("slug") or _kb.DEFAULT_BOARD
        try:
            path = _kb.kanban_db_path(board=slug).expanduser()
            resolved = str(path.resolve())
            if resolved in seen or not path.exists():
                continue
            seen.add(resolved)
            conn = _kbc.connect(board=slug)
            try:
                total += count_running_tasks(conn)
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
        except Exception:
            # Unknown occupancy: refuse to allocate rather than under-count.
            return None
    return total


def spawnable_pending_ids(conn: sqlite3.Connection) -> list[str]:
    """Ids of this board's spawnable cards waiting for a worker, head of line first.

    Everything named here is something a free slot would really have started,
    which is what makes a ceiling deferral reportable: "6 cards were ready and
    nothing could start" is actionable, "the queue is non-empty" is not.
    """
    return [row["id"] for row in _spawnable_lane_rows(conn)]


def _any_spawnable_review(
    conn: sqlite3.Connection,
    review_rows: list[sqlite3.Row],
    *,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
) -> bool:
    """Mirror review dispatch gates before reserving ready-lane capacity.

    Unavailable profile metadata retains the historic fail-open behavior. A
    review row that :func:`_dispatch_lane_task` would refuse this tick — its
    assignee already at the per-profile cap, respawn-guarded, or parked by
    the author-exclusion guard — cannot
    consume the reservation, so it must not withhold capacity from an
    otherwise ready task (one such row would pin ``ready_budget`` to 0).
    """
    if not review_rows:
        return False
    profile_exists = _profile_exists_fn()
    running = per_profile_running or {}
    for row in review_rows:
        assignee = row["assignee"]
        if not assignee:
            continue
        if profile_exists is not None and not profile_exists(assignee):
            continue
        if per_profile_cap is not None and running.get(assignee, 0) >= per_profile_cap:
            continue
        if _self_review_reason(conn, row["id"], assignee) is not None:
            continue
        if check_respawn_guard(conn, row["id"], lane="review") is None:
            return True
    return False


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    """``kanban.default_assignee`` when it names a real profile this home may
    claim (``kanban.dispatch_profiles`` gated, same predicate as the spawn
    gate). Otherwise ``None`` so an unassigned shared-board card is never
    written to. When the profiles module isn't importable trust the
    operator's config: the downstream check still buckets a missing profile
    as nonspawnable."""
    name = (default_assignee or "").strip() or None
    if name:
        profile_exists = _profile_exists_fn()
        if profile_exists is not None and not profile_exists(name):
            return None
    return name


# The dispatch lock has been released here. Fire the tick observer strictly OUTSIDE the single-writer
# critical section (#56066 sweeper finding / #64231 disposition): a slow subscriber must never extend the
# lock hold and stall a sibling dispatcher's tick.
def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
    host_budget_share: Optional[int] = None,
    lane_fair_spawn: bool = True,
    designated_pool_reserve: int = 1,
    default_max_runtime_seconds: Optional[int] = None,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`. ``lane_fair_spawn`` /
    ``designated_pool_reserve`` shape the READY lane's order only:
    :func:`_lane_fair_ready_order`. ``default_max_runtime_seconds`` (None =
    resolve ``kanban.default_max_runtime_seconds`` now) is stamped onto a card
    that carries no cap at claim time; ``0`` disables the stamp."""
    if default_max_runtime_seconds is None:
        default_max_runtime_seconds = default_max_runtime_seconds_config()
    result = DispatchResult()
    _run_reclaim_phase(
        conn, result, stale_timeout_seconds=stale_timeout_seconds,
        failure_limit=failure_limit, reconcile_orphans=reconcile_orphans, board=board,
    )
    may_spawn, spawn_budget = _tick_spawn_budget(
        conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress, board=board,
        host_budget_share=host_budget_share,
    )
    if not may_spawn:
        return result

    ready_rows = _lane_rows(conn, "ready")
    # Review rows are enumerated up front so the budget split can see whether
    # review work exists at all.
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    # Resolved BEFORE the review reservation so the reservation can see which
    # review rows the lane loop would refuse this tick.
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            per_profile_running[prow["assignee"]] = int(prow["n"])
    # Review-lane reservation: the ready loop runs first and would otherwise
    # consume the ENTIRE shared budget, starving reviews under a sustained ready
    # backlog. When spawnable review work exists and there is any budget, hold
    # one slot back.
    ready_budget = spawn_budget
    if spawn_budget is not None and spawn_budget > 0 and _any_spawnable_review(
        conn, review_rows,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
    ):
        ready_budget = max(spawn_budget - 1, 0)
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
        default_max_runtime_seconds=default_max_runtime_seconds,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    # Fair lane ordering (ruling t_b2865b89): the fairness passes only REORDER the
    # ready rows — the budget, the per-profile cap and every other guard still
    # apply in the loop below. Knob off returns ``ready_rows`` untouched, so the
    # tick is byte-identical to the straight priority walk.
    if lane_fair_spawn:
        ready_order = _lane_fair_ready_order(
            conn, ready_rows,
            budget=ready_budget,
            designated_pool_reserve=designated_pool_reserve,
            per_profile_cap=per_profile_cap,
            per_profile_running=per_profile_running,
        )
    else:
        ready_order = ready_rows
    spawned = 0
    for row in ready_order:
        if ready_budget is not None and spawned >= ready_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee so an unassigned task doesn't
            # park in 'ready' forever.
            if not default_assignee or not _apply_default_assignee(
                conn, row["id"], default_assignee, dry_run=dry_run,
            ):
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = default_assignee
            result.auto_assigned_default.append(row["id"])
        if _dispatch_lane_task(conn, row, row_assignee, result, lane="ready", **lane_kwargs):
            spawned += 1

    # A review agent (sdlc-review) approves (→ done) or requests changes
    # (→ ready/todo). Review spawns share max_spawn with ready tasks. The loop
    # checks the FULL shared ``spawn_budget`` — the reservation above caps the
    # ready lane, it grants no extra capacity here.
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        if _dispatch_lane_task(conn, row, row["assignee"], result, lane="review", **lane_kwargs):
            spawned += 1
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def lane_fair_ready_config(kanban_cfg: Optional[dict] = None) -> tuple[bool, int]:
    """``(lane_fair_spawn, designated_pool_reserve)`` from the ``kanban`` block.

    Production defaults are ``(True, 1)`` (``config_defaults.DEFAULT_CONFIG``).
    ``kanban_cfg=None`` loads config itself (mtime-cached), so the standalone
    daemon and ``hermes kanban dispatch`` resolve the fairness knobs exactly as
    the gateway dispatcher, which passes the merged block it already holds. A
    malformed value fails open to the default rather than disabling fairness or
    raising into a tick.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            kanban_cfg = (load_config_readonly() or {}).get("kanban", {}) or {}
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    raw_fair = kanban_cfg.get("lane_fair_spawn", True)
    if isinstance(raw_fair, bool):
        lane_fair = raw_fair
    elif raw_fair is None:
        lane_fair = True
    else:
        lane_fair = str(raw_fair).strip().lower() not in {"0", "false", "no", "off", ""}
    reserve = _positive_int(kanban_cfg.get("designated_pool_reserve"), 1, minimum=0)
    return lane_fair, reserve


def default_max_runtime_seconds_config(kanban_cfg: Optional[dict] = None) -> int:
    """``kanban.default_max_runtime_seconds`` — the cap stamped onto a card that carries none.

    ``0`` disables the stamp entirely (no card behaviour changes at all). ``kanban_cfg=None``
    loads config itself (mtime-cached), exactly as :func:`lane_fair_ready_config`, so the
    standalone daemon, ``hermes kanban dispatch`` and the in-gateway dispatcher all resolve the
    knob the same way. A malformed value fails open to the production default rather than
    raising into a tick or silently disabling slot recycling.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config_readonly
            kanban_cfg = (load_config_readonly() or {}).get("kanban", {}) or {}
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    # 0 is a REAL value (no stamp), so it cannot go through ``_positive_int`` (minimum=1); only a
    # missing / non-integer / negative value falls back to the production default.
    try:
        parsed = int(kanban_cfg.get("default_max_runtime_seconds", DEFAULT_MAX_RUNTIME_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RUNTIME_SECONDS
    return parsed if parsed >= 0 else DEFAULT_MAX_RUNTIME_SECONDS


def _stamp_default_max_runtime_seconds(
    conn: sqlite3.Connection, task_id: str, *, limit: int,
) -> Optional[int]:
    """Write ``limit`` onto ``tasks.max_runtime_seconds`` when the card carries none.

    Slot recycling (ruling ``t_b2865b89`` §4): the runtime sweep is gated on ``max_runtime_seconds IS
    NOT NULL``, so a card with no cap is never reaped and its spawn slot never recycles. ``IS NULL``
    in the WHERE clause (rather than ``COALESCE``) is deliberate: it makes the stamp idempotent and
    gives a card's OWN value precedence — including an explicit ``0``, the author's unlimited
    opt-out — so re-claiming a card can never overwrite an author's budget.

    Returns the value written, or ``None`` when the card already carried one. Opens its own write txn
    (the caller holds no lock at this point) and records a ``runtime_cap_defaulted`` event, so the
    defaulted cap is auditable rather than a silent column mutation.
    """
    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? "
            "WHERE id = ? AND max_runtime_seconds IS NULL",
            (int(limit), task_id),
        )
        if cur.rowcount != 1:
            return None
        _kb._append_event(conn, task_id, "runtime_cap_defaulted", {
            "max_runtime_seconds": int(limit),
            "source": "kanban.default_max_runtime_seconds",
        })
    return int(limit)


def _lane_fair_ready_order(
    conn: sqlite3.Connection,
    ready_rows: list[sqlite3.Row],
    *,
    budget: Optional[int],
    designated_pool_reserve: int = 1,
    per_profile_cap: Optional[int] = None,
    per_profile_running: Optional[dict[str, int]] = None,
) -> list[sqlite3.Row]:
    """Order the ready lane so no LANE starves behind another lane's priority.

    INTRA-BOARD ONLY. This never ranks one board against another: the cross-board
    rotation keeps reading ``_lane_rows`` order through ``head_of_line_priority``
    and ``spawnable_pending_ids``, which is why ``_lane_rows`` itself must not
    change.

    ``ready_rows`` is already the dispatch order. Walking it straight hands every
    free slot to whichever lanes filed the highest cards, so a lane whose work is
    merely ordinarily ranked — and therefore always below the designated tranche
    (``>= TRANCHE_FLOOR``) — never starts while a designated lane has a queue.
    This orders the SAME rows in three passes instead:

    * **designated reach** — one slot per lane whose head card is designated, in
      least-recently-run order, capped by ``max(budget - designated_pool_reserve,
      0)`` so the tranche cannot hold every slot while ordinary work waits;
    * **lane reach** — one slot per lane not served by the first pass, same
      order; a lane whose head is a designated card already refused by the
      ceiling is still reached through the first row of it the room test
      admits, so the lane (not the head) is what a reach pass serves;
    * **fill** — the remaining budget in global priority order.

    Lanes are served least-recently-run first: a lane with no ``task_runs`` row
    at all sorts BEFORE every lane that has one, then the oldest
    ``started_at`` wins. Ties keep the lane's first-seen (priority) order, so
    the result is deterministic. WITHIN a lane the order is untouched — the
    tier still decides WHICH card of a lane runs.

    Every pass skips a row whose assignee is already at
    ``kanban.max_in_progress_per_profile``; ``per_profile_running`` already
    counts this tick's own allocations (see ``_dispatch_lane_task``), so a lane
    at its cap is not "reached" and cannot spend the slot a capped neighbour
    would waste. The designated ceiling is lifted when NO spawnable ordinary
    row waits (an all-designated board still gets the whole budget).

    Returned rows are the allocation order first, then every remaining row in
    its original order: a row the passes could not place is still tried by the
    caller, never silently dropped from the tick. ``budget None`` = uncapped —
    the reach order still applies an ordering, but nothing is held back.
    """
    if not ready_rows:
        return ready_rows
    from hermes_cli import kanban_priority_policy as policy

    tranche_floor = int(policy.TRANCHE_FLOOR)
    reserve = designated_pool_reserve if isinstance(designated_pool_reserve, int) else 1
    reserve = max(reserve, 0)
    running = dict(per_profile_running or {})

    def _lane_of(row: sqlite3.Row) -> str:
        # No assignee is its own bucket: the caller may still resolve it through
        # kanban.default_assignee, and an unassigned card must not be able to
        # consume a named lane's reach.
        return row["assignee"] or ""

    def _designated(row: sqlite3.Row) -> bool:
        return int(row["priority"] or 0) >= tranche_floor

    def _at_cap(lane: str) -> bool:
        if per_profile_cap is None or not lane:
            return False
        return running.get(lane, 0) >= per_profile_cap

    # Lanes in lane-local order: ready_rows is the dispatch order, so the first
    # row seen for a lane is that lane's head.
    lanes: dict[str, list[sqlite3.Row]] = {}
    for row in ready_rows:
        lanes.setdefault(_lane_of(row), []).append(row)

    # ONE read-only query: the newest run start per lane. A lane that never ran
    # has no row and ranks before every lane that has.
    last_run: dict[str, int] = {}
    for r in conn.execute(
        "SELECT profile, MAX(started_at) AS last FROM task_runs "
        "WHERE profile IS NOT NULL GROUP BY profile"
    ):
        last_run[str(r["profile"])] = int(r["last"] or 0)

    def _lru_key(lane: str) -> tuple[int, int]:
        if lane in last_run:
            return (1, last_run[lane])
        return (0, 0)

    lru_lanes = sorted(lanes, key=_lru_key)  # stable: ties keep priority order
    designated_lanes = [lane for lane in lru_lanes if _designated(lanes[lane][0])]

    # The ceiling only exists while a spawnable ORDINARY row waits: if every
    # waiting row is designated (or the ordinary ones are all capped), holding a
    # slot back would idle it for nobody.
    ordinary_waiting = any(
        not _designated(row) and not _at_cap(_lane_of(row)) for row in ready_rows
    )
    designated_cap: Optional[int] = None if budget is None else budget
    if budget is not None and ordinary_waiting:
        designated_cap = max(budget - reserve, 0)

    ordered: list[sqlite3.Row] = []
    placed: set[str] = set()
    served: set[str] = set()
    spent = 0
    designated_spent = 0

    def _room(row: sqlite3.Row) -> bool:
        if budget is not None and spent >= budget:
            return False
        if _at_cap(_lane_of(row)):
            return False
        if designated_cap is not None and _designated(row) and designated_spent >= designated_cap:
            return False
        return True

    def _take(row: sqlite3.Row) -> None:
        nonlocal spent, designated_spent
        lane = _lane_of(row)
        ordered.append(row)
        placed.add(row["id"])
        served.add(lane)
        spent += 1
        if per_profile_cap is not None and lane:
            running[lane] = running.get(lane, 0) + 1
        if _designated(row):
            designated_spent += 1

    def _has_budget() -> bool:
        return budget is None or spent < budget

    # Pass 1 — designated reach: one slot per designated lane, LRU first.
    for lane in designated_lanes:
        if not _has_budget():
            break
        head = lanes[lane][0]
        if _room(head):
            _take(head)
    # Pass 2 — lane reach: one slot per lane pass 1 did not serve. A lane's
    # HEAD can be refused here without the lane being skipped: when a designated
    # head's ceiling slot is already spent but the lane also holds ordinary work,
    # reach the lane through the first row of it that ``_room`` admits. Testing
    # only the head would drop the lane for the whole reach phase and hand the
    # slot back to a lane already served — two lanes with designated heads and
    # ordinary work waiting would then never both be reached at budget 2
    # (ruling t_b2865b89 P-Reach, §7.2).
    for lane in lru_lanes:
        if not _has_budget():
            break
        if lane in served:
            continue
        for row in lanes[lane]:
            if _room(row):
                _take(row)
                break
    # Pass 3 — fill: the rest of the budget in global priority order.
    for row in ready_rows:
        if not _has_budget():
            break
        if row["id"] in placed:
            continue
        if _room(row):
            _take(row)

    ordered.extend(row for row in ready_rows if row["id"] not in placed)
    return ordered


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.
    Defaults: rotate at 2 MiB, keep one backup (``.log.1``); both overridable
    from ``config.yaml``.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    kanban_cfg = kanban_cfg or {}
    max_bytes = _positive_int(kanban_cfg.get("worker_log_rotate_bytes"), DEFAULT_LOG_ROTATE_BYTES, minimum=1)
    backup_count = _positive_int(kanban_cfg.get("worker_log_backup_count"), DEFAULT_LOG_BACKUP_COUNT, minimum=0)
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``: ``<log>`` → ``<log>.1``,
    older generations shift up to ``backup_count``.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(backup_count, DEFAULT_LOG_BACKUP_COUNT, minimum=0)
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        with contextlib.suppress(OSError):
            if oldest.exists():
                oldest.unlink()
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            with contextlib.suppress(OSError):
                src.rename(_rotated_log_path(log_path, generation + 1))
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Interpreter-bound Hermes CLI invocation (``hermes_cli.main`` is the
    console-script target — there is no top-level ``hermes`` package).

    ``-P`` (3.11+) keeps the child's *working directory* off ``sys.path``. A
    worker's cwd is its task workspace, and a workspace that is a worktree of
    THIS repository (every card working on Hermes itself) carries its own
    ``hermes_cli/`` and ``pm/``; with cwd first on ``sys.path`` the child
    imported the worktree's copy, resolved its project root to the worktree,
    found no committed dependency environment under that install key and died
    at boot with ``no dependency environment is committed for this install``
    — on every retry, leaving the card ``ready`` and looking unattended
    (measured 2026-10-18: 24 crashed spawns across skills-refactor +
    migrations). ``PYTHONPATH`` already carries the running install's root
    (``_propagate_module_import_root``), so nothing is lost.
    """
    return [sys.executable, "-P", "-m", "hermes_cli.main"]


def _is_module_hermes_argv(cmd: list[str]) -> bool:
    """Is this argv the interpreter-bound module form (``-P -m hermes_cli.main``)?

    The prefix check tolerates the ``-P`` that keeps the worker's cwd off
    ``sys.path``; the rest of the vector (``-p``, ``--cli``, ``chat -q ...``)
    is appended by ``_worker_argv`` after the module pair.
    """
    tail = cmd[1:]
    if tail[:1] == ["-P"]:
        tail = tail[1:]
    return tail[:2] == ["-m", "hermes_cli.main"]


def _propagate_module_import_root(cmd: list[str], env: dict[str, str]) -> None:
    """Put the running install's package root on a module-form worker's path.

    ``_resolve_hermes_argv`` proves ``hermes_cli`` importable in THIS process,
    where a store-python shim has the repo root on ``sys.path`` in-process;
    the spawned child runs the bare ``sys.executable`` from the task workspace
    with a scrubbed ``PYTHONPATH`` and cannot import the package the parent
    just proved importable — it dies before any work and the board
    auto-blocks (#122299, #122487, #122500). Same-interpreter child, so the
    root is version-safe to propagate; ``hermes_cli.main``'s own bootstrap
    then owns dependency activation as usual. A resolved shim path owns its
    imports and is left alone. Same pin cron's external worker uses (#112729).
    """
    if not _is_module_hermes_argv(cmd):
        return
    from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath

    pin_hermes_tree_on_pythonpath(env, Path(__file__).resolve().parents[1])


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _kb._IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    return [command + ext for ext in raw.split(";") if ext]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    On Windows ``shutil.which`` may search the current directory before PATH
    for bare names — unsafe for a dispatcher. Only explicit PATH entries are
    considered; empty / ``.`` entries are skipped.
    """
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and (_kb._IS_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """argv for a resolved Hermes executable path. Windows batch shims
    (``.cmd``/``.bat``) are unsafe as argv[0] because the argument vector
    includes task-derived values; prefer the module form."""
    if _kb._IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv for ``Popen``: ``$HERMES_BIN``
    (path-like -> absolute; bare names keep PATH semantics, never a
    same-directory file), then the running interpreter's ``sys.executable -m
    hermes_cli.main`` (exactly this install; also covers shim-less cron,
    systemd ``User=``, launchd), then ``which("hermes")`` (Windows: safe PATH
    search, batch shims fall back to the module form) only when ``hermes_cli``
    is not importable. The module argv must win over PATH: a PATH-first lookup
    lets an attacker-planted ``hermes`` shadow the running install (#111569).
    Mirrors ``gateway.run._resolve_hermes_bin``; local because ``hermes_cli``
    sits below ``gateway`` in the dependency order.
    """
    import importlib.util
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    try:
        if importlib.util.find_spec("hermes_cli") is not None:
            return _module_hermes_argv()
    except Exception:
        pass

    hermes_bin = _safe_which_no_cwd("hermes") if _kb._IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    When ``max_runtime_seconds`` exceeds the terminal tool's default timeout,
    raise only the child's default so a long command isn't killed by the
    generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


@contextlib.contextmanager
def _worker_profile_scope(hermes_home: str, *, bind_home: bool = True):
    """Bind an assigned profile's runtime scope (secrets + terminal policy, optionally home) for
    one dispatch-side read or spawn-env build.

    The dispatcher runs detached from any turn, so nothing binds a profile for it: ``load_config``,
    the toolset probes' ``get_secret`` reads and ``build_subprocess_env``'s passthrough resolution
    all fall back to the LAUNCH profile's ambient ``os.environ`` / ``TERMINAL_*``. Binding was
    previously conditional on ``is_multiplex_active()``, so on a single-profile host a worker for
    profile B was built entirely from the dispatcher's own environment.

    ``bind_home=False`` for the spawn-env build: which variables may cross into a child is the
    DISPATCHER's ``terminal.env_passthrough`` policy (#109494, read through the home override) —
    only their VALUES come from the assignee's scope, so that branch binds the secret scope alone.
    Toolset resolution binds the home and the terminal policy, as it always has.

    The secret mapping is never widened: a profile that is not this process's own home gets its own
    ``.env`` + external sources ONLY, while the launch home keeps its established
    env-over-``.env`` precedence (``launch_secret_scope``) so systemd / ``op run`` injection still
    resolves for a standalone dispatcher.
    """
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import get_process_hermes_home, reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope
    from tui_gateway.launch_profile_policy import launch_secret_scope, launch_terminal_env

    home = Path(hermes_home)
    is_launch_home = str(home.resolve()) == str(Path(get_process_hermes_home()).resolve())
    home_token = secret_token = terminal_token = None
    try:
        home_token = set_hermes_home_override(str(home)) if bind_home else None
        secret_token = set_secret_scope(
            launch_secret_scope(home) if is_launch_home else build_profile_secret_scope(home),
            profile_home=None if is_launch_home else str(home))
        terminal_token = install_profile_terminal_scope(
            home, env_overlay=launch_terminal_env() if is_launch_home else None) if bind_home else None
        yield
    finally:
        if terminal_token is not None:
            reset_terminal_scope(terminal_token)
        if secret_token is not None:
            reset_secret_scope(secret_token)
        if home_token is not None:
            reset_hermes_home_override(home_token)


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Resolved at dispatch time and passed as an explicit ``--toolsets`` pin so
    worker startup cannot fall back to a stale root/active-profile config or a
    profile whose top-level ``toolsets`` is only the kanban orchestrator
    surface. ``model_tools`` still appends the task-scoped kanban lifecycle
    tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        with _worker_profile_scope(hermes_home):
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        return toolsets or None
    except Exception as exc:
        _kb._log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None


_retagged_workspace_roots: set[str] = set()


def _retag_legacy_worker_sessions(workspaces_root_path: str) -> None:
    """Reclaim pre-tag worker rows in state.db so they leave the session lists.

    Best-effort: the durable gate is ``state_meta`` in
    ``retag_kanban_worker_sessions``; the in-process set avoids reopening
    state.db on every spawn. A tick must never fail because a session DB was
    busy or missing.
    """
    if workspaces_root_path in _retagged_workspace_roots:
        return
    try:
        from hermes_state_registry import acquire, release_or_close

        # Inside the gateway the dispatcher shares the process's registry handle; a bare
        # SessionDB() here was one more writer connection on the same state.db (#100896).
        db = acquire()
        try:
            db.retag_kanban_worker_sessions(workspaces_root_path)
        finally:
            release_or_close(db)
        _retagged_workspace_roots.add(workspaces_root_path)
    except Exception as exc:
        _kb._log.debug("kanban worker: legacy session retag skipped (%s)", exc)


def _worker_argv(task: Task, profile_arg: str, hermes_home: Optional[str]) -> list[str]:
    """Build the ``hermes -p <profile> --cli ... chat -q ...`` worker command."""
    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # A worker must NEVER boot the interactive TUI: its no-TTY bail-out
        # exits 0 without doing the task → "protocol violation" every attempt.
        "--cli",
        # Workers run under a profile-scoped HERMES_HOME and so see that
        # profile's shell-hook allowlist; pass --accept-hooks explicitly so
        # configured hooks still register.
        "--accept-hooks",
    ]
    # One `--skills X` pair per name: easier to read in `ps` and avoids quoting
    # ambiguity if a skill name contains unusual chars.
    for sk in task.skills or ():
        if sk:
            cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
        # Pin the provider too so the worker resolves the model against the
        # intended backend (model X with provider Y is the classic board-stall).
        if task.provider_override:
            cmd.extend(["--provider", task.provider_override])
    # Independent of the model override — a task can run the profile's own
    # model at a different depth.
    if task.reasoning_effort:
        cmd.extend(["--reasoning", task.reasoning_effort])
    worker_toolsets = _resolve_worker_cli_toolsets(hermes_home)
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.extend(["chat", "-q", f"work kanban task {task.id}"])
    # goal_mode rides the same `-q` path: cli.py runs the judge loop there too, so the
    # worker log keeps its live tool feed (forcing -Q blanked it).
    return cmd


def _open_worker_log(task: Task, board: Optional[str]):
    """Append-mode per-task log (a re-run on unblock appends, never overwrites),
    rotated first. Anchored at the board root (not the shared kanban root) so
    `hermes kanban log` reads its own file and boards sharing task ids don't
    collide."""
    log_dir = _kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)
    return open(log_path, "ab")


def _restart_safe_worker_argv(task: Task, command: list[str]) -> list[str]:
    """Wrap a systemd-hosted dispatcher's worker in the shared restart-safe scope.

    Kanban workers are long-lived agentic runs that outlive the dispatcher
    tick, so they never take cron's degraded mode under the managed gateway:
    ``require_restart_safe_scope=True`` makes the helper raise
    ``RestartSafeScopeUnavailable`` there (an infrastructure spawn failure the
    dispatcher does not charge to the card). Under any other systemd unit
    (``Type=oneshot`` dispatch timers, #113612) ``outlives_parent=True`` gets the
    worker its own scope so the unit's cgroup teardown cannot kill it.
    """
    from tools.process_registry import restart_safe_gateway_child_argv

    if task.current_run_id is None:
        # Outside managed systemd this is harmless, but a managed dispatch must
        # never mint an untraceable worker.  Check topology through the shared
        # helper first, using a placeholder suffix that cannot be launched.
        dispatch = restart_safe_gateway_child_argv(
            command,
            unit_suffix=f"kanban-{task.id}-run-missing",
            require_restart_safe_scope=True,
            outlives_parent=True,
        )
        if dispatch.mode != "in_process":
            raise RuntimeError(
                "cannot create restart-safe systemd scope for Kanban worker: "
                "the claimed task has no current run id"
            )
        return command

    return restart_safe_gateway_child_argv(
        command,
        unit_suffix=f"kanban-{task.id}-run-{task.current_run_id}",
        require_restart_safe_scope=True,
        outlives_parent=True,
    ).argv


def _default_spawn(task: Task, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the child's PID so the dispatcher can detect crashes before the
    claim TTL expires; completion is still observed via the worker's own
    ``complete`` / ``block`` transitions. ``board`` pins the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root to the
    board the task was claimed from, so workers cannot see other boards.
    """
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

    profile_arg = normalize_profile_name(task.assignee)

    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import _is_routed_home, build_subprocess_env, strip_launch_profile_env

    try:
        profile_home = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # No profile dir (isolated test fixtures) — the CLI resolves it from
        # HERMES_PROFILE (set below) instead.
        profile_home = None

    # Scrub for a ROUTED home, not only under multiplex: the authority test is "does this worker act
    # for another profile", exactly as served_profile_child_env decides it (tools/environments/local.py).
    # Gating on the gateway-wide flag left B's worker inheriting the dispatcher's own OPENAI_API_KEY and
    # systemd-injected tokens on every single-profile host.
    routed = bool(profile_home) and _is_routed_home(profile_home)
    # build_subprocess_env's secret scrub resolves terminal.env_passthrough vars through get_secret(),
    # which without a bound scope reads the LAUNCH profile's ambient environment for a worker spawned
    # on B's behalf (and raises under multiplex) — so bind B's secret scope around the build.
    with (_worker_profile_scope(profile_home, bind_home=False) if profile_home
          else contextlib.nullcontext()):
        env = build_subprocess_env(
            scrub_secrets=is_multiplex_active() or routed,
            inherit_profile_home=True,
        )
    # The dispatcher is detached from every conversation; its worker must never
    # inherit routing mirrored by a previous gateway turn.
    from gateway.session_context import _VAR_MAP
    for key in _VAR_MAP:
        env.pop(key, None)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml:
    # without it the child's get_hermes_home() falls back to the DEFAULT
    # profile root because `hermes -p` applies its override before
    # hermes_constants is imported.
    if profile_home:
        env["HERMES_HOME"] = profile_home
        # A multiplexer dispatching for another profile must not hand it the launch
        # profile's .env settings / TERMINAL_* policy — a standalone dispatcher never would.
        strip_launch_profile_env(env, profile_home)
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    # Tag the session `kanban` so session-browsing surfaces filter it out by
    # source instead of rendering one sidebar row per attempt.
    env["HERMES_SESSION_SOURCE"] = "kanban"
    # TERMINAL_CWD takes precedence over process cwd in file_tools and
    # build_context_files_prompt; without it relative writes land in the gateway
    # user's home and workers load the gateway's AGENTS.md. file_tools rejects
    # relative / sentinel values, so only set a real absolute directory.
    # Pin TERMINAL_CWD to the task's workspace so the worker's file tools and context-file loader anchor on
    # the workspace, not whatever cwd the dispatching gateway happened to export. The worker subprocess is
    # already launched with cwd=workspace, but TERMINAL_CWD takes precedence over the process cwd in both
    # file_tools._resolve_base_dir (#41312 — relative write_file paths were landing in the gateway user's
    # home) and build_context_files_prompt (#34619 — workers loaded the dispatching gateway's AGENTS.md
    # instead of the task's). Setting it to the workspace fixes both: the workspace is where the task's work
    # actually happens.
    if workspace and os.path.isabs(workspace) and os.path.isdir(workspace):
        env["TERMINAL_CWD"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Goal-loop mode (Ralph-style /goal judge loop in cli.py quiet-mode path).
    # Only set when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
    for var in ("TERMINAL_TIMEOUT", "TERMINAL_MAX_FOREGROUND_TIMEOUT"):
        override = _worker_terminal_timeout_env(task.max_runtime_seconds, env.get(var))
        if override is not None:
            env[var] = override
    # Pin the board DB + workspaces root so the worker's kanban paths still
    # match after `hermes -p` rewrites HERMES_HOME (symlink / Docker layouts).
    env["HERMES_KANBAN_DB"] = str(_kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(_kb.workspaces_root(board=board))
    _retag_legacy_worker_sessions(env["HERMES_KANBAN_WORKSPACES_ROOT"])
    # Board slug — defense-in-depth pin if a path is resolved without the
    # DB / workspaces env vars.
    env["HERMES_KANBAN_BOARD"] = _kb._normalize_board_slug(board) or _kb.get_current_board()
    # kanban_comment reads HERMES_PROFILE for its default author; `-p` alone
    # doesn't set the env var.
    env["HERMES_PROFILE"] = profile_arg
    # This is the grant boundary: the dispatcher assigned this new worker's task.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    env.pop(DELEGATED_CHILD_ENV_MARKER, None)
    # `--cli` is the highest-precedence TUI override; dropping HERMES_TUI covers
    # older hermes builds on PATH that predate the flag's precedence.
    env.pop("HERMES_TUI", None)

    cmd = _worker_argv(task, profile_arg, env.get("HERMES_HOME"))
    # The module argv must carry the import context that made it resolvable:
    # the shim's in-process path injection is invisible to the bare child.
    _propagate_module_import_root(cmd, env)
    # A worker spawned by a managed systemd gateway must leave the gateway's
    # cgroup before startup; otherwise restarting the service kills the worker
    # that is performing the handoff.
    cmd = _restart_safe_worker_argv(task, cmd)
    from tools.process_registry import systemd_user_bus_env
    env = systemd_user_bus_env(env)
    log_f = _open_worker_log(task, board)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _kb._IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    # Intentionally NOT closing log_f: the child keeps writing after return;
    # the OS-level FD stays open in the child until it exits.
    if _kb._IS_WINDOWS:
        _live_worker_procs[proc.pid] = proc
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds; exits cleanly on
    SIGINT / SIGTERM so it is systemd-friendly. ``stop_event`` and ``on_tick``
    are test hooks. Each tick resolves ``kanban.max_in_progress`` exactly like
    the gateway dispatcher and ``hermes kanban dispatch`` — the standalone
    daemon must not be the one uncapped entry point.

    This loop is ALSO a long-lived consumer (ruling ``t_8fed34c8``): it records its own content
    fingerprint at startup, so a checkout rewritten under it is detected by the gate in
    :func:`dispatch_once` -- exactly as it is for the gateway's embedded dispatcher. A one-shot
    ``hermes kanban dispatch`` needs no record: it imports fresh from disk and cannot be stale.
    """
    import threading

    try:
        from hermes_cli import tree_fingerprint

        tree_fingerprint.record_boot()
    except Exception:
        # A daemon that cannot fingerprint its tree keeps ticking (fail open); the gate no-ops.
        logger.debug("kanban daemon: could not record the boot tree fingerprint", exc_info=True)

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only on the main thread — tests call this inline from
    # worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, _handle)

    while not stop_event.is_set():
        try:
            # Re-resolved every tick (config load is mtime-cached) so operator
            # edits apply without a restart.
            max_in_progress = resolve_max_in_progress(configured_max_in_progress())
            with contextlib.closing(_kbc.connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    max_in_progress=max_in_progress,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                with contextlib.suppress(Exception):
                    on_tick(res)
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# Late-bound origin namespace (see module docstring); imported LAST so this
# module is fully populated before ``kanban_db`` imports from it.
from hermes_cli import kanban_db as _kb
from hermes_cli import kanban_db_connect as _kbc
from hermes_cli import kanban_db_workspace as _kbw
from hermes_cli import substrate_access as _sa

# The tree-skew RETIREMENT doors (card t_028ad743) live in a topical sibling and are re-exported
# here: ``dispatch_once`` calls them by name at tick time, and the tests patch this module. Imported
# after ``_kb`` for the same reason -- the sibling reaches back into ``kanban_db`` at CALL time.
from hermes_cli.kanban_db_dispatch_retire import (
    _close_cleared_cards_on,
    _close_cleared_tick_yield_cards,
    _retire_cleared_tree_skew_cards,
    _retire_cleared_substrate_cards,
    _retire_tree_skew_actors,
    _tree_skew_actor_pid_from_key,
    _tree_skew_actor_provenance,
)
