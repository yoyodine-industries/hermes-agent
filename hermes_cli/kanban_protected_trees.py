"""The host's protected DEPLOYMENT-SOURCE checkouts, read from the guard itself.

Why this module exists (card t_b151a247, ruling on that card): a worker used to be
able to work inside the canonical unit checkout (``/opt/hermes_sandbox/<repo>``),
reach ``done``, and leave the tree dirty. ``gate.py`` check 1 then refuses every
deploy sourced from that checkout, with no waiver, so the stall is silent: the card
reads ``done`` while prod stops receiving. The completion boundary therefore has to
ask one question — *is the tree this card worked in a deployment source, and is it
dirty?* — and this module answers the first half of it.

SINGLE SOURCE OF TRUTH. The set of protected roots is the shared-tree guard's own
declaration (``_SHARED_SOURCE_ROOTS`` in ``guard-shared-trees.py``), imported from
the deployed file, never copied here. Two copies of a security rule drift, and the
copy next to the completion path is the one nobody re-reads. The same reasoning is
recorded on the sibling seam card t_81c03c9a, which needs the identical predicate.

FAIL-OPEN BY DESIGN. A missing/unreadable guard, a guard that does not declare the
list, or an unreadable home config means "no protected roots" — the gate then does
nothing at all. The guard itself is fail-open for the same reason (``fail_closed:
false`` in the hook config): a broken perimeter must never brick every lane's
completion. The protection that matters is the hook refusing the write; this module
only lets the completion boundary refuse to *believe* a write that got through.

WHERE THE GUARD IS FOUND, in order:

  HERMES_PROTECTED_TREES_GUARD        explicit override (tests, an alternate estate)
  the ACTIVE HOME's own hook wiring   ``<HERMES_HOME>/config.yaml`` ->
                                      ``hooks.pre_tool_call[].command`` naming
                                      ``guard-shared-trees`` — the place the fleet
                                      already declares this guard, so a home that
                                      does not wire it gets no gate
  the deployed artifact               ``/opt/hermes_prod/yaan-majordomo/ops-checks/
                                      guard-shared-trees.py`` — where the
                                      ``ops-checks`` deploy unit lands it
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

#: The guard's file name. Matched, never spelled as a root list — the point of this
#: module is that there is exactly ONE copy of the roots.
GUARD_NAME = "guard-shared-trees.py"

#: The deployed artifact beside its own ledger, as the `ops-checks` unit lands it.
DEPLOYED_GUARD = "/opt/hermes_prod/yaan-majordomo/ops-checks/guard-shared-trees.py"

#: Explicit override, so a test (or an alternate estate) never reads the live tree.
ENV_GUARD = "HERMES_PROTECTED_TREES_GUARD"

#: Cached per process: the roots and the module that declared them.
_ROOTS_CACHE: list[str] | None = None
_MODULE_CACHE = None


def _active_home() -> Path:
    home = os.environ.get("HERMES_HOME")
    return Path(home) if home else Path.home() / ".hermes"


def _guard_from_home_config() -> str | None:
    """The guard this home's own hook chain names, if it names one.

    Read as DATA (``hooks.pre_tool_call`` rows), not by importing the config module:
    this runs on the completion hot path and must not drag the config stack in.
    """
    config = _active_home() / "config.yaml"
    try:
        import yaml  # noqa: PLC0415 - optional at this seam, and cheap when cached

        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - absent/unreadable/unparseable => no wiring
        return None
    hooks = data.get("hooks") if isinstance(data, dict) else None
    rows = hooks.get("pre_tool_call") if isinstance(hooks, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        command = row.get("command") if isinstance(row, dict) else None
        if not isinstance(command, str) or GUARD_NAME not in command:
            continue
        # The command is a shell word list; the guard is the token that names it.
        for token in command.replace('"', " ").replace("'", " ").split():
            if token.endswith(GUARD_NAME) and os.path.isabs(token):
                return token
    return None


def guard_script() -> str | None:
    """The guard file to read the roots from, or ``None`` when this host has none."""
    return (
        os.environ.get(ENV_GUARD)
        or _guard_from_home_config()
        or (DEPLOYED_GUARD if os.path.exists(DEPLOYED_GUARD) else None)
    )


def _load_guard():
    """Import the guard module for its DECLARATION only (it is stdlib-only and its
    module body defines constants; ``main()`` runs only under ``__main__``)."""
    global _MODULE_CACHE
    if _MODULE_CACHE is not None:
        return _MODULE_CACHE
    path = guard_script()
    if not path or not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("_hermes_shared_tree_guard", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - any failure is "no declaration" (fail-open)
        return None
    _MODULE_CACHE = module
    return module


def shared_source_roots() -> list[str]:
    """The guard's own declared shared-source (deployment) roots; empty when unknown.

    Honours the guard's own ``GUARD_SHARED_TREES`` override, because a fixture that
    points the guard at decoy roots must point this reader at them too — otherwise the
    two sides would disagree about a rule they share.
    """
    global _ROOTS_CACHE
    if _ROOTS_CACHE is not None:
        return _ROOTS_CACHE
    import json

    override = os.environ.get("GUARD_SHARED_TREES")
    if override:
        try:
            value = json.loads(override)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            _ROOTS_CACHE = list(value)
            return _ROOTS_CACHE
    module = _load_guard()
    roots = getattr(module, "_SHARED_SOURCE_ROOTS", None) if module else None
    if not isinstance(roots, (list, tuple)):
        roots = []
    _ROOTS_CACHE = [r for r in roots if isinstance(r, str) and r]
    return _ROOTS_CACHE


def reset_cache() -> None:
    """Drop the memoized guard/roots (a test that re-points the guard must see it)."""
    global _ROOTS_CACHE, _MODULE_CACHE
    _ROOTS_CACHE = None
    _MODULE_CACHE = None


def _resolved(path: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(path))
    except (TypeError, ValueError):
        return path


def shared_source_root(path: str | None) -> str | None:
    """The protected root that ``path`` sits inside, or ``None``.

    Containment is judged on RESOLVED paths, so a workspace reached through a symlink
    is still inside the tree it really points into.
    """
    if not path:
        return None
    target = _resolved(path)
    if not target or target == os.sep:
        return None
    for root in shared_source_roots():
        resolved = _resolved(root)
        if resolved and (target == resolved or target.startswith(resolved + os.sep)):
            return root
    return None
