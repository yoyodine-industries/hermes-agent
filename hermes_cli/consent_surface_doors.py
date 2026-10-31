"""The three WRITE-BOUNDARY doors' shared plumbing for the consent-surface clause.

Design of record: ``t_cb9259fc`` -- ``DESIGN-t_cb9259fc-surface-write-boundary.md``,
sections 3a-3c (the doors) and 6 (ordering), D3 (floor on the terminal/exec doors,
standalone yolo-proof clause on the file door) and D5 (inert without an owning card).

WHY this module exists. The class itself lives in ``hermes_cli.consent_surface`` (pure,
no I/O: *which* surface is this target). The decision, the refusal text, the deduped
receipt and the park live in ``hermes_cli.consent_surface_guard``. What is left over is
the same three-step dance at every door:

    classify the door's target -> ask the guard -> turn the answer into the door's shape

Written once here, so the FILE door (a path list), the TERMINAL door (a command string)
and the EXEC door (a code string) cannot drift apart on *what counts as a target*, on
when the clause is inert, or on what a refusal looks like. No surface semantics are
added here: the class comes from the classifier and the verdict from the guard.

DOOR SHAPES (the pinned contract the call sites rely on):

    file_denial(paths, ...)            -> str | None          # the file door's error string
    command_denial(command, ...)       -> dict | None         # the terminal floor's deny dict
    text_denial(code, ...)             -> dict | None         # the exec floor's deny dict
    hit_for_paths / hit_for_command / hit_for_text             # the raw SurfaceHit, for tests

INERT-FIRST. ``consent_gate_active`` is asked BEFORE any path is resolved or classified.
That keeps the fleet-wide cost of this clause at one environment lookup for the ordinary
case (an interactive session, a cron job not spawned by the dispatcher -- no owning
kanban card, so no consent authority to consult and the existing human gates stand), and
it is what makes "INERT without an owning card" (D5) true of the doors rather than only
of the guard.

NO FENCE-POST RECEIPTS. ``refuse_surface_write`` is the one call that writes the
``consent_refused`` event, the comment and the park, so a door never has to know about
the board at all -- and a store outage logs there instead of raising here (a guard must
never take a tool call down).

RESIDUALS, named not hidden (design section 9): a surface path built dynamically at
runtime evades the EXEC door's literal scan (``hit_for_text``); anything the script then
shells out is covered by the TERMINAL door, which is not a text scan. A custom hermes
home that carries no ``.hermes`` path segment is not recognised unless a caller passes
``hermes_home=`` explicitly; the standard layout, and any ``hermes-agent`` checkout, is.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, Optional

from hermes_cli import consent_surface as cs
from hermes_cli import consent_surface_guard as guard

logger = logging.getLogger(__name__)

__all__ = [
    "command_denial",
    "file_denial",
    "hit_for_command",
    "hit_for_paths",
    "hit_for_text",
    "text_denial",
]

_TOKEN_RE = re.compile(r"""(['"])(?P<path>[^'"\n]{3,}?)\1""")

#: ``open(<literal>, "<mode>")`` where the mode WRITES. The mode is what separates a
#: write from the read this clause must never refuse (``open(".../config.yaml")``).
#: An ``os.path.expanduser(...)`` wrapper around the literal is the ordinary way a script
#: spells a home path, so it is read as part of the target rather than treated as dynamic.
_PY_OPEN_WRITE_RE = re.compile(
    r"open\s*\(\s*(?:(?:os\s*\.\s*path\s*\.\s*|pathlib\s*\.\s*)?expanduser\s*\(\s*)?"
    r"(?P<q>['\"])(?P<path>[^'\"\n]+)(?P=q)\s*\)?\s*,\s*"
    r"""(?P<q2>['"])(?P<mode>[^'"\n]*)(?P=q2)"""
)
#: A literal handed to a call that WRITES it: ``Path("<p>").write_text(...)`` and the
#: destination slot of the copy/move families.
_PY_DEST_CALL_RE = re.compile(
    r"Path\s*\(\s*(?P<q1>['\"])(?P<p1>[^'\"\n]+)(?P=q1)\s*\)\s*\.\s*"
    r"(?:write_text|write_bytes|unlink|touch|mkdir|rmdir|rename|replace)\s*\("
    r"|(?:shutil|os)\s*\.\s*(?:copy|copy2|copyfile|move|replace|rename)\s*\(\s*[^,()\n]+,\s*"
    r"(?P<q2>['\"])(?P<p2>[^'\"\n]+)(?P=q2)"
)
#: A literal bound to a name, either bare or through ``Path(...)``.
_PY_BINDING_RE = re.compile(
    r"""(?P<name>[A-Za-z_]\w*)\s*=\s*(?:Path\s*\(\s*)?(?P<q>['"])(?P<path>[^'"\n]+)(?P=q)"""
)
#: Calls that WRITE through a name, so a bound literal written on a later line is seen.
_PY_WRITE_VIA_NAME_RE = (
    r"(?:open\s*\(\s*{}|{}\s*\.\s*(?:write_text|write_bytes|write|unlink|replace|rename|"
    r"touch|mkdir|rmdir)\s*\(|(?:copy|copy2|copyfile|move)\s*\(\s*{}|"
    r"(?:replace|rename|remove|unlink)\s*\(\s*{})"
)


# --- roots (see the module docstring's residual note) -------------------------

def _roots(hermes_home: Optional[str], install_root: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """The roots to classify against: an explicit one, else ``None`` (lexical markers).

    A caller may pass either; nothing is read from the environment here. The doors call
    with no roots on purpose, so classification follows the shape of the path -- see the
    module docstring on why ``HERMES_HOME`` is NOT forwarded as the home: in a worker
    session it is the PROFILE home, and treating it as the root would name a profile
    ``config.yaml`` with the root token.
    """
    return (hermes_home or None), (install_root or None)


def _resolved(target: Any) -> str:
    """Absolute, ``~``-expanded, symlink-resolved spelling of a path target.

    Resolution is the CALLER's job per the classifier's contract, and it is the caller's
    here because it needs the filesystem: a symlink into the live home must not smuggle a
    write past the class.
    """
    text = str(target)
    try:
        return os.path.realpath(os.path.expanduser(text))
    except (OSError, ValueError):  # absurd path: classify the raw text rather than fail
        return text


def _active(ctx: Any) -> bool:
    """Whether the clause applies at all (inert without an owning card, D5)."""
    try:
        return bool(guard.consent_gate_active(ctx))
    except Exception:  # a guard must never take a tool call down
        logger.debug("consent-surface doors: activity check failed; staying inert", exc_info=True)
        return False


# --- the three target readers -------------------------------------------------

def hit_for_paths(
    paths: Iterable[Any],
    *,
    hermes_home: Optional[str] = None,
    install_root: Optional[str] = None,
) -> "cs.SurfaceHit | None":
    """The FIRST surface among ``paths``, or ``None``. The first hit wins, so a
    multi-file patch is refused on the surface it names first."""
    home, install = _roots(hermes_home, install_root)
    for target in paths or ():
        if target is None:
            continue
        text = str(target).strip()
        if not text:
            continue
        try:
            hit = cs.classify_path(_resolved(text), hermes_home=home, install_root=install)
        except Exception:  # classification is total; a bug must not wedge a write
            logger.debug("consent-surface doors: classification failed for %r", text, exc_info=True)
            continue
        if hit is not None:
            return hit
    return None


def hit_for_command(
    command: Any,
    *,
    hermes_home: Optional[str] = None,
    install_root: Optional[str] = None,
) -> "cs.SurfaceHit | None":
    """The surface a shell command would WRITE, or ``None``. Reads return ``None``:
    the write shape has to be present before a path in the command is a destination."""
    text = str(command or "")
    if not text.strip():
        return None
    home, install = _roots(hermes_home, install_root)
    try:
        return cs.classify_command(text, text, hermes_home=home, install_root=install)
    except Exception:
        logger.debug("consent-surface doors: command classification failed", exc_info=True)
        return None


def hit_for_text(
    code: Any,
    *,
    hermes_home: Optional[str] = None,
    install_root: Optional[str] = None,
) -> "cs.SurfaceHit | None":
    """Best-effort scan of a Python script for a surface write (the EXEC door, section 3c).

    Two deterministic passes, no inference:

    1. every LINE through the command classifier, which catches the shell shapes a script
       can spawn (``subprocess.run("cp ... ~/Library/LaunchAgents/")``);
    2. the Python file API -- ``open(<literal>, "<write mode>")``, and a literal bound to
       a name (``p = Path("...")``) that is later written through that name.

    A literal that is only READ is not a hit. A path assembled at runtime is a named
    residual (module docstring); ``terminal()`` calls the script issues hit the TERMINAL
    door, which does not depend on this scan.
    """
    text = str(code or "")
    if not text.strip():
        return None
    home, install = _roots(hermes_home, install_root)

    # 1. shell shapes reachable from the script: whole lines, and the quoted literals on
    #    them -- a script that shells out carries its command inside a string
    #    (``subprocess.run("cp x ~/Library/LaunchAgents/")``), which a line-level tokenise
    #    cannot see. Reads return None at this layer.
    for candidate in _shell_candidates(text):
        try:
            hit = cs.classify_command(candidate, candidate, hermes_home=home, install_root=install)
        except Exception:
            hit = None
        if hit is not None:
            return hit

    # 2a. open(<literal>, "<mode>") -- only the modes that WRITE.
    for match in _PY_OPEN_WRITE_RE.finditer(text):
        mode = match.group("mode")
        if not any(flag in mode for flag in ("w", "a", "x", "+")):
            continue
        hit = _classify_path(match.group("path"), home, install)
        if hit is not None:
            return hit

    # 2b. a literal a WRITING call is applied to (``Path("<p>").write_text(...)``, the
    #     destination slot of ``shutil.copy``/``os.replace``).
    for match in _PY_DEST_CALL_RE.finditer(text):
        literal = match.group("p1") or match.group("p2")
        if not literal:
            continue
        hit = _classify_path(literal, home, install)
        if hit is not None:
            return hit

    # 2c. a literal bound to a name, written through that name on a later line.
    for match in _PY_BINDING_RE.finditer(text):
        name = match.group("name")
        writes = re.search(_PY_WRITE_VIA_NAME_RE.format(*([re.escape(name)] * 4)), text)
        if writes is None:
            continue
        hit = _classify_path(match.group("path"), home, install)
        if hit is not None:
            return hit
    return None


def _shell_candidates(text: str):
    """The strings a script could hand to a shell: each LINE, then each quoted literal."""
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped not in seen:
            seen.add(stripped)
            yield stripped
    for match in _TOKEN_RE.finditer(text):
        literal = match.group("path").strip()
        if len(literal) >= 3 and literal not in seen:
            seen.add(literal)
            yield literal


def _classify_path(literal: str, home: Optional[str], install: Optional[str]) -> "cs.SurfaceHit | None":
    try:
        return cs.classify_path(_resolved(literal), hermes_home=home, install_root=install)
    except Exception:
        logger.debug("consent-surface doors: literal classification failed for %r", literal, exc_info=True)
        return None

# --- the doors' denial shapes -------------------------------------------------

def _refusal(hit: Any, ctx: Any) -> tuple[bool, dict]:
    """``(approved, result)`` from the guard, total: a failed decision FAILS CLOSED."""
    try:
        result = guard.refuse_surface_write(hit, ctx)
    except Exception:  # the guard is already total; this is the belt to its braces
        logger.exception("consent-surface doors: the guard failed; refusing the write")
        surface = str(getattr(hit, "surface", "") or "")
        target = str(getattr(hit, "path", "") or getattr(hit, "scope_token", "") or "")
        return False, {
            "approved": False,
            "consent_surface": True,
            "receipt": False,
            "message": (
                f"BLOCKED (consent surface): the consent-surface guard could not decide the "
                f"write to {surface!r} ({target or '(unspecified)'}) and fails closed. "
                "See the logs for the cause."
            ),
        }
    if not isinstance(result, dict):
        return False, {
            "approved": False,
            "consent_surface": True,
            "receipt": False,
            "message": "BLOCKED (consent surface): the consent-surface guard returned "
                       "no verdict and fails closed.",
        }
    return bool(result.get("approved")), result


def file_denial(
    paths: Iterable[Any],
    *,
    ctx: Any = None,
    verb: str = "Write",
    hermes_home: Optional[str] = None,
    install_root: Optional[str] = None,
) -> Optional[str]:
    """The FILE door's clause: the refusal string for a surface write, else ``None``.

    ``verb`` only labels the door in the log line; the message itself is the guard's
    (surface, target, owning card, its references, the scope token to file).
    """
    if not _active(ctx):
        return None
    hit = hit_for_paths(paths, hermes_home=hermes_home, install_root=install_root)
    if hit is None:
        return None
    approved, result = _refusal(hit, ctx)
    if approved:
        return None
    logger.warning("consent-surface %s refused: %s -> %s", verb,
                   getattr(hit, "surface", "?"), getattr(hit, "path", ""))
    return str(result.get("message") or (
        "BLOCKED (consent surface): this write targets a consent-gated live surface "
        "and the owning card carries no approval covering it."))


def command_denial(
    command: Any,
    *,
    ctx: Any = None,
    hermes_home: Optional[str] = None,
    install_root: Optional[str] = None,
) -> Optional[dict]:
    """The TERMINAL floor: the deny dict for a surface command, else ``None``."""
    if not _active(ctx):
        return None
    hit = hit_for_command(command, hermes_home=hermes_home, install_root=install_root)
    if hit is None:
        return None
    approved, result = _refusal(hit, ctx)
    if approved:
        return None
    logger.warning("consent-surface command refused: %s -> %s",
                   getattr(hit, "surface", "?"), getattr(hit, "scope_token", ""))
    return result


def text_denial(
    code: Any,
    *,
    ctx: Any = None,
    hermes_home: Optional[str] = None,
    install_root: Optional[str] = None,
) -> Optional[dict]:
    """The EXEC floor: the deny dict for a script that writes a surface, else ``None``."""
    if not _active(ctx):
        return None
    hit = hit_for_text(code, hermes_home=hermes_home, install_root=install_root)
    if hit is None:
        return None
    approved, result = _refusal(hit, ctx)
    if approved:
        return None
    logger.warning("consent-surface code refused: %s -> %s",
                   getattr(hit, "surface", "?"), getattr(hit, "scope_token", ""))
    return result
