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

FAIL-OPEN, BUT NEVER SILENTLY. A guard this module cannot read yields NO protected
roots, so the completion boundary does nothing — deliberately: a broken perimeter
must never brick every lane's completion (the guard itself is fail-open for the same
reason, ``fail_closed: false`` in the hook config). The protection that matters is
the hook refusing the write; this module only lets the completion boundary refuse to
*believe* a write that got through.

What is NOT acceptable is the two shapes being indistinguishable. "This host has no
guard" and "the guard was here yesterday and is gone" both used to read as an empty
root list, so a deleted guard silently narrowed a security check — no error, no log,
no refusal, anywhere. ``guard_diagnosis()`` separates them: an EXPECTED guard (named
by the home's config, or absent from a deployed dest that exists) that cannot be read
is reported in one stderr line, at most once per process, while the roots stay empty
and the gate stays open. A host that has never had a guard stays legitimately empty.

WHERE THE GUARD IS FOUND, in order:

  HERMES_PROTECTED_TREES_GUARD        explicit override (tests, an alternate estate)
  the ACTIVE HOME's own hook wiring   ``<HERMES_HOME>/config.yaml`` ->
                                      ``hooks.pre_tool_call[].command`` naming
                                      ``guard-shared-trees`` — the place the fleet
                                      already declares this guard, so a home that
                                      does not wire it gets no gate
  the deployed artifact               ``/opt/hermes_prod/ops-checks/guard-shared-trees.py``
                                      — the ``ops-checks`` deploy unit's live dest. It
                                      is no longer reached through the retired
                                      ``yaan-majordomo`` shim farm (a symlink into
                                      that dest), so the constant names the dest.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

#: The guard's file name. Matched, never spelled as a root list — the point of this
#: module is that there is exactly ONE copy of the roots.
GUARD_NAME = "guard-shared-trees.py"

#: The deployed artifact, as the `ops-checks` deploy unit lands it (``projects.conf``
#: row 256). This is the DEST, not the shim farm that used to alias it.
DEPLOYED_GUARD = "/opt/hermes_prod/ops-checks/guard-shared-trees.py"

#: Explicit override, so a test (or an alternate estate) never reads the live tree.
ENV_GUARD = "HERMES_PROTECTED_TREES_GUARD"

#: Which step of the resolution order answered — carried so a report can say why the
#: path it names is an EXPECTED one.
SOURCE_ENV = "env"
SOURCE_CONFIG = "config"
SOURCE_DEPLOYED = "deployed"

#: ``guard_diagnosis()`` states. ONLY ``GUARD_OK`` yields a non-empty root list; every
#: other state keeps the gate FAIL-OPEN (it refuses nothing) and says what was lost.
GUARD_OK = "ok"                           # read, and it declares roots
GUARD_ABSENT = "absent"                   # no guard here, and none is expected
GUARD_MISSING = "missing"                 # declared, or its dest exists; no file
GUARD_UNREADABLE = "unreadable"           # the file is there; importing it raised
GUARD_NO_DECLARATION = "no-declaration"   # imported, but declares no root list

#: Cached per process: the ONE read of the guard, and the roots it declared.
_DIAGNOSIS: dict | None = None
_ROOTS_CACHE: list[str] | None = None

#: Faults already reported in THIS process, keyed ``(state, guard)``: the point is one
#: line per fault, not one line per completion.
_WARNED: set[tuple[str, str]] = set()


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


def _resolution() -> tuple[str | None, str]:
    """``(guard path, which step answered)`` — the resolution order in the docstring."""
    override = os.environ.get(ENV_GUARD)
    if override:
        return override, SOURCE_ENV
    wired = _guard_from_home_config()
    if wired:
        return wired, SOURCE_CONFIG
    if os.path.exists(DEPLOYED_GUARD):
        return DEPLOYED_GUARD, SOURCE_DEPLOYED
    return None, ""


def guard_script() -> str | None:
    """The guard file to read the roots from, or ``None`` when this host has none."""
    return _resolution()[0]


def _import_guard(path: str) -> tuple[object | None, str]:
    """Import the guard module for its DECLARATION only (it is stdlib-only and its
    module body defines constants; ``main()`` runs only under ``__main__``).

    Returns ``(module, "")`` or ``(None, why)``. The reason is carried OUT, never
    raised: the gate stays fail-open, and an unexplained silence is the defect.
    """
    try:
        spec = importlib.util.spec_from_file_location("_hermes_shared_tree_guard", path)
        if spec is None or spec.loader is None:
            return None, f"{path} is not importable as a module"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - reported as a state, never raised
        return None, f"{type(exc).__name__}: {exc}"
    return module, ""


def _diagnose() -> dict:
    """Read the guard once and say what this host has — the ONE read both halves share.

    The distinction that matters is ``expected``: FALSE means "this host carries no
    guard" (a legitimate fail-open choice), TRUE means "a guard is declared here, or
    its deploy dest is here, and it cannot be read" — a protection that has silently
    stopped protecting.
    """
    global _DIAGNOSIS
    if _DIAGNOSIS is not None:
        return _DIAGNOSIS

    path, source = _resolution()
    state, expected, roots, detail = GUARD_ABSENT, False, [], ""

    if path is None:
        # Nothing names a guard at all. Two very different hosts look like this, and
        # the DEPLOY DEST tells them apart: the `ops-checks` unit ships into that
        # directory, so a directory with no guard in it LOST its guard, while a host
        # that never carried the unit has no directory to look at.
        dest = os.path.dirname(DEPLOYED_GUARD)
        if os.path.isdir(dest):
            state, expected = GUARD_MISSING, True
            detail = (
                f"no guard is wired by this home and {DEPLOYED_GUARD} is absent, though "
                f"its deploy dest {dest}/ exists: the protected-tree check is protecting "
                "NOTHING"
            )
        else:
            detail = (
                f"this host carries no shared-tree guard ({dest}/ absent), so there are "
                "no protected roots to read"
            )
    elif not os.path.exists(path):
        if source == SOURCE_ENV:
            # An override naming nothing is the caller's own declaration of "no guard"
            # (tests and alternate estates do exactly this) — not a regression.
            detail = f"{ENV_GUARD} names {path}, which does not exist: no protected roots to read"
        else:
            state, expected = GUARD_MISSING, True
            detail = (
                f"{source} names {path}, which does not exist: the protected-tree check "
                "is protecting NOTHING"
            )
    else:
        module, error = _import_guard(path)
        if module is None:
            state, expected = GUARD_UNREADABLE, True
            detail = (
                f"{path} exists but could not be read ({error}): the protected-tree "
                "check is protecting NOTHING"
            )
        else:
            declared = getattr(module, "_SHARED_SOURCE_ROOTS", None)
            if isinstance(declared, (list, tuple)):
                roots = [r for r in declared if isinstance(r, str) and r]
                state = GUARD_OK
                detail = f"root declaration read from {path} ({len(roots)} root(s))"
            else:
                state, expected = GUARD_NO_DECLARATION, True
                detail = (
                    f"{path} declares no _SHARED_SOURCE_ROOTS: the protected-tree check "
                    "is protecting NOTHING"
                )

    _DIAGNOSIS = {
        "state": state,
        "expected": expected,
        "guard": path,
        "source": source,
        "roots": roots,
        "detail": detail,
    }
    return _DIAGNOSIS


def _report(diagnosis: dict) -> None:
    """One line on stderr per ``(state, guard)`` in this process — the receipt for a loss."""
    key = (diagnosis["state"], diagnosis["guard"] or "")
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(
        f"hermes: protected-trees guard {diagnosis['state']}: {diagnosis['detail']}",
        file=sys.stderr,
    )


def guard_diagnosis() -> dict:
    """What this host's guard resolves to, and whether that answer is trustworthy.

    ``state`` is one of the ``GUARD_*`` constants; ``expected`` is what separates the
    two ways of being empty. ``roots`` is the declaration (empty unless ``GUARD_OK``),
    so a caller can both gate on it and report the loss. An EXPECTED guard that cannot
    be read is announced once on stderr — the gate itself stays OPEN either way,
    because a broken perimeter must never brick every lane's completion.
    """
    diagnosis = _diagnose()
    if diagnosis["expected"] and diagnosis["state"] != GUARD_OK:
        _report(diagnosis)
    return {**diagnosis, "roots": list(diagnosis["roots"])}


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
    _ROOTS_CACHE = list(guard_diagnosis()["roots"])
    return _ROOTS_CACHE


def reset_cache() -> None:
    """Drop the memoized read (a test that re-points the guard must see it).

    The once-per-process report is deliberately NOT reset: it keys on ``(state,
    path)``, so re-reading the SAME fault must not repeat the line.
    """
    global _ROOTS_CACHE, _DIAGNOSIS
    _ROOTS_CACHE = None
    _DIAGNOSIS = None


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
