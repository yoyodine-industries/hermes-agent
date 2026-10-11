"""Tree identity — which ``hermes_cli`` tree dispatches this board, and may THIS process move it?

The dispatcher and the CLI are normally the same install, but on a host where a second
checkout sits earlier on ``PATH`` (its own editable finder binds ``hermes_cli`` to that
checkout) a bare ``hermes kanban …`` runs a code line with none of the current guards and
writes the board anyway — measured 2026-09-27, card t_70e91ef2: the unguarded tree minted
five cards on a throwaway board and still wrote the two-field ``witness|start`` fingerprint.

The durable fix is a fence, not a rule: the dispatching process RECORDS the tree it
dispatches from at boot (:func:`record_dispatching_tree`), and every estate-mutating CLI
verb compares the calling process's own tree against that record
(:func:`assert_dispatching_tree`, the fail-closed entry point the decision names, and the
CLI-facing :func:`dispatching_tree_refusal` that renders its message). Same tree = allow. A
different tree = refuse fail-closed, loudly, naming both trees.

Deliberate boundaries:

* **The fence lives at the CLI seam only.** Reads are never checked, and the dispatcher's
  in-process writes (``kanban_db`` / ``kanban_db_dispatch``) never pass through it — the
  dispatcher owns the record and must never refuse itself. ``tests/hermes_cli/
  test_kanban_tree_identity.py`` asserts neither module grows an import of this one.
* **An absent record allows with a warning** (a host with no gateway is not an error), and a
  corrupt/unreadable record is treated as absent rather than bricking the estate's own board.
* **No waiver, and no env var that waives the fence** (card t_73155b4a, arm b). This module used
  to document ``HERMES_ALLOW_TREE_SKEW`` as the exemption for the hermes-update / R5 installer
  door. Measurement, 2026-09-29: no caller needs it. The var could only ever be read by a tree
  that CARRIES this module, and the trees the installer door actually runs carry none — its
  PATH-first resolutions (the SEV1 node's ``hermes_repair_nodes._sev1_bin``, at
  ``scripts/hermes_repair_nodes.py:3839``, returns ``shutil.which("hermes")`` — and is distinct
  from that same module's ``_resolve_hermes_bin``, which deliberately prefers the live checkout
  over PATH (card t_937fc14e); ``ops-checks/update-watchdog.py --hermes-bin`` defaults to bare
  ``hermes``) land on the self-updater's isolated runtime venv, whose editable finder binds
  ``hermes_cli`` to ``~/.hermes/installs/<id>/environments/<id>/workspace`` — measured: no
  ``tree_identity.py`` there, and no fence call in that tree's ``kanban.py``, so a waiver could
  not be consulted. A waiver is also the wrong repair for those callers: both resolve their CLI
  from ``PATH``, so exempting them would exempt board writes from whatever tree ``PATH`` happens
  to lead to — the 2026-09-27 failure (an unguarded copy minted five cards) this fence exists to
  stop. A caller that must write the board must run the dispatching tree; if that tree ever
  acquires this module the refusal is loud at the CLI seam (card t_4be81cec owns that reach gap
  and re-opening the door deliberately).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Stable, greppable prefix of every refusal. ``_err`` prints it verbatim, so a refusal is
#: one stderr line that starts with exactly this token.
REFUSAL_PREFIX = "tree-skew refusal:"

#: Record filename inside the kanban home (``<kanban_home>/kanban/dispatcher_tree.json``).
RECORD_FILENAME = "dispatcher_tree.json"

RECORD_VERSION = 1


class TreeSkewRefusal(RuntimeError):
    """Raised by :func:`assert_dispatching_tree`: this process's tree is not the dispatcher's.

    ``str(exc)`` always starts with :data:`REFUSAL_PREFIX`.
    """

#: Estate-mutating CLI verbs. A superset of ``kanban._DELEGATED_CHILD_DENIED_ACTIONS``, the
#: sibling fence that answers a different question ("may a delegate_task child write the board?")
#: about the same set of writes; a test asserts this fence is never NARROWER than that one, and
#: ``test_estate_mutating_verbs_are_fenced`` guards it verb by verb.
#: Deliberately WIDER in one verb (card t_73155b4a, F2): ``set-model`` retargets which model a
#: card dispatches on, which is an estate write a skewed CLI must not perform, but it is absent
#: from the delegated-child set, whose width is platform-stl's ruling. The divergence is named
#: here (and in the test) rather than closed from this side.
ESTATE_MUTATING_ACTIONS: frozenset[str] = frozenset({
    "init", "create", "swarm", "assign", "reclaim", "reassign", "link", "unlink",
    "claim", "comment", "attach", "attach-rm", "complete", "edit", "block",
    "set-contract", "set-model", "schedule", "unblock", "promote", "archive",
    "dispatch", "daemon",
    "repair", "heartbeat", "notify-subscribe", "notify-unsubscribe", "specify", "decompose",
    "request-review", "request-changes", "reopen", "reopen-review", "gc", "defcon", "bulk-approvals",
})

#: ``hermes kanban boards …`` sub-actions that write board metadata or move the current board.
ESTATE_MUTATING_BOARD_ACTIONS: frozenset[str] = frozenset({
    "create", "new", "rm", "remove", "delete", "switch", "use", "rename",
    "set-default-workdir", "set-priority-policy", "import",
})


# --- resolution ------------------------------------------------------------------------

def current_tree() -> Path:
    """The ``hermes_cli`` tree THIS process is running from (never a hardcoded path)."""
    return Path(__file__).resolve().parent.parent


def record_path() -> Path:
    """``<kanban_home>/kanban/dispatcher_tree.json`` — machine-global, like the board itself."""
    from hermes_cli import kanban_db as kb

    return kb.kanban_home() / "kanban" / RECORD_FILENAME


def _tree_forms(value: Any) -> Optional[tuple[str, str]]:
    """``(resolved, raw)`` spellings of a tree path; ``None`` when it is not a usable path."""
    try:
        resolved = str(Path(value).expanduser().resolve())
    except (OSError, TypeError, ValueError):
        return None
    raw = str(value)
    if os.name == "nt":
        return resolved.lower(), raw.lower()
    return resolved, raw


def _same_tree(left: Any, right: Any) -> bool:
    """Do two path spellings name the same tree? Resolved form first, raw form as a fallback."""
    lhs, rhs = _tree_forms(left), _tree_forms(right)
    if lhs is None or rhs is None:
        return False
    return lhs[0] == rhs[0] or lhs[1] == rhs[1]


def _process_start_time(pid: Optional[int] = None) -> Optional[float]:
    """``psutil`` process create time (this process by default); ``None`` when unavailable."""
    try:
        import psutil

        return float(psutil.Process(os.getpid() if pid is None else pid).create_time())
    except Exception:
        return None


def _iso(epoch: Optional[float]) -> Optional[str]:
    """ISO-8601 UTC spelling of a POSIX timestamp — the human-readable half of the record."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


# --- the record ------------------------------------------------------------------------

def read_record() -> Optional[dict]:
    """The recorded dispatcher identity, or ``None`` when absent/unreadable/malformed."""
    path = record_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        logger.warning("tree-skew: dispatcher tree record at %s is unreadable; treating it "
                       "as absent", path)
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("tree-skew: dispatcher tree record at %s is not valid JSON; treating "
                       "it as absent", path)
        return None
    if not isinstance(data, dict) or not str(data.get("tree") or "").strip():
        logger.warning("tree-skew: dispatcher tree record at %s names no tree; treating it "
                       "as absent", path)
        return None
    return data


def record_dispatching_tree(*, purpose: str = "gateway-dispatcher") -> bool:
    """Record THIS process as the tree that dispatches the board. Overwrite, never append.

    Called by every process that takes up dispatching (the gateway's embedded dispatcher at
    boot). Best-effort: a failure is logged, never raised — an unwritable kanban home must not
    stop the gateway from coming up.
    """
    path = record_path()
    start_epoch = _process_start_time()
    payload = {
        "version": RECORD_VERSION,
        "tree": str(current_tree()),
        "pid": os.getpid(),
        "process_start": start_epoch,
        "started_at": _iso(start_epoch),
        "recorded_at": time.time(),
        "purpose": purpose,
    }
    try:
        from hermes_constants import mkdir_under_hermes_home

        mkdir_under_hermes_home(path.parent)
    except Exception:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.debug("tree-skew: cannot create %s for the dispatcher record", path.parent,
                         exc_info=True)
            return False
    try:
        from utils import atomic_json_write

        atomic_json_write(path, payload, mode=0o600)
        return True
    except OSError:
        logger.debug("tree-skew: could not write the dispatcher record to %s", path, exc_info=True)
        return False


# --- the fence -------------------------------------------------------------------------

def cli_action_moves_estate(action: Optional[str], boards_action: Optional[str] = None) -> bool:
    """Does this CLI verb MOVE the estate? Reads are never fenced."""
    if action == "boards":
        return (boards_action or "list") in ESTATE_MUTATING_BOARD_ACTIONS
    return action in ESTATE_MUTATING_ACTIONS


def dispatching_tree_refusal(action: Optional[str],
                             boards_action: Optional[str] = None) -> Optional[str]:
    """``None`` when this call may proceed; else the refusal message to print and fail on.

    Order matters: a read is never checked at all, and only a record that names a DIFFERENT
    tree refuses. There is no env var that waives the check — see the module docstring.
    """
    if not cli_action_moves_estate(action, boards_action):
        return None

    record = read_record()
    ours = current_tree()
    path = record_path()
    if record is None:
        logger.warning("tree-skew: no dispatcher tree record at %s; allowing '%s' from %s "
                       "(a host with no gateway is not an error)", path, action, ours)
        return None

    theirs = str(record.get("tree") or "")
    if _same_tree(ours, theirs):
        return None

    pid = record.get("pid")
    pid_part = f"dispatcher pid={pid}" if pid is not None else "dispatcher pid=unknown"
    return (f"{REFUSAL_PREFIX} this CLI runs from {ours} but the dispatcher for this board "
            f"runs from {theirs} ({pid_part}, record={path}); refusing "
            f"'{action}' - run the CLI from the dispatcher's own tree (its venv's bin) or ask "
            f"the ops head to fix this shell's PATH.")


def assert_dispatching_tree(action: Optional[str] = None,
                            boards_action: Optional[str] = None) -> None:
    """Fail-closed form of the fence: raise unless THIS tree may move the estate.

    The named entry point of the decision (card t_70e91ef2): any non-CLI caller that would
    move the board calls this and lets :class:`TreeSkewRefusal` propagate. The CLI itself uses
    :func:`dispatching_tree_refusal` so it can print the message as one stderr line and return
    a non-zero exit status instead of a traceback.
    """
    message = dispatching_tree_refusal(action, boards_action)
    if message:
        raise TreeSkewRefusal(message)
