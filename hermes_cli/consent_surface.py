"""The consent-gated SURFACE class: one pure classifier, no I/O.

Design of record: ``t_cb9259fc`` -- ``DESIGN-t_cb9259fc-surface-write-boundary.md``
section 2 (the class), D1 (closed set of six surfaces) and D2 (scope tokens).
This module is task 0 of the implementation.  It decides ONLY *which* consent-gated
surface a write would touch and *what scope token* an approval row must carry to
cover it.  It does not decide whether consent exists, does not refuse, does not
park a card, and does not import the guard modules: the FILE door (task 2), the
TERMINAL/EXEC doors (task 3) and the census (task 4) wire it in and own the
decision / refusal / receipt half (``surface_decision`` and friends, task 1).

D1 -- the class is CLOSED at six surfaces (``SURFACES``):

    hermes-config        $HERMES_HOME/config.yaml, profiles/*/config.yaml
    hermes-env           $HERMES_HOME/.env, profiles/*/.env
    hermes-soul          $HERMES_HOME/SOUL.md, profiles/*/SOUL.md
    hermes-plugin        $HERMES_HOME/plugins/**, <install>/plugins/**
    launchd-plist        ~/Library/LaunchAgents/*.plist
    hermes-cli-config    argv ``hermes config set|unset|edit``

Adding a surface is a design change, not a patch.

D2 -- scope tokens (what ``SurfaceHit.scope_token`` carries, and what a covering
APPROVED row must carry; coverage is the decision half's job, not this module's):

    hermes-config        hermes-config:root            ($HERMES_HOME/config.yaml)
    hermes-config        hermes-config:<profile>
    hermes-env           hermes-env:root               ($HERMES_HOME/.env)
    hermes-env           hermes-env:<profile>
    hermes-soul          hermes-soul:root              ($HERMES_HOME/SOUL.md)
    hermes-soul          hermes-soul:<profile>
    hermes-plugin        hermes-plugin:<plugin-name>
    launchd-plist        launchd-plist:<basename>
    hermes-cli-config    hermes-config:<dotted.key>   (``hermes-config`` for ``edit``)

Root-home files carry ``:root``, never a bare token.  The bare tokens
(``hermes-config``, ``hermes-env``, ``hermes-soul``) are the whole-domain COVERAGE
PARENTS -- an approval row carrying one covers every profile's file.  The refusal
message names the token the operator has to file, so a root write must name
``:root``: telling them to file bare ``hermes-config`` would silently license every
lane's config.yaml.  ``profiles/default/...`` is a different spelling and keeps
``:default``.  The root home is one seat's home, not the fleet's.

WHY PURE.  One definition of "this path is a surface" has to serve the file door,
the terminal door and the execute-code door, and it has to be unit-testable
without a hermes home, a database or a live gateway.  So classification reads
nothing: no filesystem, no ``os.environ``, no DB, no network, at import or call
time.  Consequences worth knowing:

* ``~``, ``$HOME``, ``${HERMES_HOME}`` are expanded LEXICALLY -- the shape of the
  path is what identifies the surface, never the identity of the current user.
* Symlink resolution is the CALLER's job.  A door that accepts a path from a
  model should ``os.path.realpath`` it before calling ``classify_path``, so a
  symlink into ``$HERMES_HOME`` cannot smuggle a write past the class.
* A custom ``HERMES_HOME`` / install root that carries none of the structural
  markers below can be supplied explicitly (``hermes_home=``, ``install_root=``)
  by a caller that is allowed to read the environment.  An explicit root is
  AUTHORITATIVE: it is the only root that classifies, and the generic segment scan
  (a ``.hermes`` segment, or a ``hermes-agent`` one) runs only when no root was
  supplied.  Otherwise every repo or test fixture containing ``.hermes/`` would be
  refused -- the standard shape for hermes fixtures, and a false-positive class the
  FILE door would then hit fleet-wide.  With an explicit root the marker branch
  still runs, so the same file spelled ``~/.hermes/config.yaml`` or
  ``$HERMES_HOME/config.yaml`` classifies too.  Defaults (no argument) are lexical:
  a ``.hermes`` path segment marks the hermes home, and a segment named
  ``hermes-agent`` (the fork's checkout directory name) marks the install root.

The one non-lexical input is DELIBERATE REUSE, not a second opinion.  For in-place
edits the terminal approval layer already owns the patterns (``tools.approval_detection``:
"in-place edit of Hermes config/env ...", "overwrite system file via redirection/tee"),
so this module CONSUMES that verdict through a guarded lazy import instead of
re-deriving the pattern corpus.  Reuse alone is not enough: the shared corpus names
only the ROOT ``config.yaml``/``.env`` spellings, and the measurement on ``t_cb9259fc``
shows redirection/tee/``sed -i`` into ``profiles/*/SOUL.md``, ``plugins/**`` and a
LaunchAgents plist passes it.  So the write SHAPE is tokenised here as well --
redirection, ``tee``, the destination of ``cp``/``mv``/``install``, and the in-place
flag -- and the shared verdict is folded in as an additional authority, never as a
replacement.  The import is guarded and never affects the module's importability.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

__all__ = ["SURFACES", "SurfaceHit", "classify_command", "classify_path"]


# --- D1: the closed class ---------------------------------------------------

SURFACES: frozenset[str] = frozenset(
    {
        "hermes-config",
        "hermes-env",
        "hermes-soul",
        "hermes-plugin",
        "launchd-plist",
        "hermes-cli-config",
    }
)


@dataclass(frozen=True)
class SurfaceHit:
    """A write that must carry a covering APPROVED row.

    ``surface``     one of :data:`SURFACES`
    ``scope_token`` the D2 token a covering approval row must carry
    ``path``        the offending path, or ``None`` for an argv-only hit
    ``reason``      human sentence for the refusal message
    """

    surface: str
    scope_token: str
    path: str | None
    reason: str

    def __post_init__(self) -> None:
        if self.surface not in SURFACES:
            raise ValueError(
                f"unknown consent surface {self.surface!r}; the class is closed at "
                f"{sorted(SURFACES)} (adding one is a design change)"
            )


# --- lexical path helpers ---------------------------------------------------

_QUOTES = "\"'"
_HERMES_HOME_DIRNAME = ".hermes"
_INSTALL_DIRNAME = "hermes-agent"
_ROOT_TOKEN = "root"
_HOME_ROOT_MARKERS = frozenset(
    {"~", "$HOME", "${HOME}", "$HERMES_HOME", "${HERMES_HOME}", "~hermes"}
)


def _strip_quotes(token: str) -> str:
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in _QUOTES:
        return text[1:-1]
    return text


def _normalise(path: str) -> str:
    """Collapse ``//``, ``.`` and ``..`` without touching the filesystem."""
    raw = _strip_quotes(str(path))
    if not raw:
        return ""
    return posixpath.normpath(raw)


def _segments(normalised: str) -> tuple[str, ...]:
    return tuple(seg for seg in normalised.split("/") if seg not in ("", "."))


def _under_root(segs: tuple[str, ...], root: str | None) -> tuple[str, ...] | None:
    """Segments below an explicit root, or ``None`` when the path is not under it."""
    if not root:
        return None
    base = _segments(_normalise(root))
    if base and len(segs) > len(base) and segs[: len(base)] == base:
        return segs[len(base) :]
    return None


def _under_marker(segs: tuple[str, ...], marker: str) -> tuple[str, ...] | None:
    """Segments below ``~``/``$HERMES_HOME`` written literally, or ``None``.

    ``~/.hermes/config.yaml`` and ``$HERMES_HOME/config.yaml`` name the same file as
    an absolute path under an explicit root, so both spellings have to survive the
    root-matching branch, which is the only one left when a root is supplied.
    """
    if not segs or segs[0] not in _HOME_ROOT_MARKERS:
        return None
    rest = segs[1:]
    if rest and rest[0] == marker:
        rest = rest[1:]
    return rest


def _hermes_relative(
    segs: tuple[str, ...], hermes_home: str | None
) -> tuple[str, ...] | None:
    """Segments below the hermes home, or ``None`` when the path is not under it.

    An explicit home is AUTHORITATIVE.  When one is supplied the generic
    ``.hermes``-segment scan is skipped, because it would otherwise classify every
    ``.hermes`` inside an unrelated tree (a repo, a test fixture) as the live home
    -- the false-positive class that would make the FILE door refuse fixtures.
    """
    if hermes_home:
        rel = _under_root(segs, hermes_home)
        if rel is not None:
            return rel
        return _under_marker(segs, _HERMES_HOME_DIRNAME)
    for idx, seg in enumerate(segs):
        if seg == _HERMES_HOME_DIRNAME:
            return segs[idx + 1 :]
    return _under_marker(segs, _HERMES_HOME_DIRNAME)


def _install_relative(
    segs: tuple[str, ...], install_root: str | None
) -> tuple[str, ...] | None:
    """Segments below the hermes install root, or ``None``.

    Same rule as the hermes home: an explicit install root is authoritative, so a
    checked-out fixture named ``hermes-agent`` elsewhere is not the live install.
    """
    if install_root:
        return _under_root(segs, install_root)
    for idx, seg in enumerate(segs):
        if seg == _INSTALL_DIRNAME and idx + 1 < len(segs):
            return segs[idx + 1 :]
    return None


def _plugin_hit(rel: tuple[str, ...], path_text: str) -> SurfaceHit | None:
    """``plugins/<name>[/...]`` -- the token names the plugin, not the file."""
    if len(rel) < 2:
        return None  # the plugins directory itself is not a write target
    name = rel[1]
    if len(rel) == 2:
        # plugins/evil.py -> the plugin is ``evil``, same as plugins/evil/.
        stem = posixpath.splitext(name)[0]
        name = stem or name
    if not name:
        return None
    return SurfaceHit(
        surface="hermes-plugin",
        scope_token=f"hermes-plugin:{name}",
        path=path_text,
        reason=f"write into the Hermes plugin tree ({name!r})",
    )


def _hermes_relative_hit(rel: tuple[str, ...], path_text: str) -> SurfaceHit | None:
    """The root home's three named files, then the per-profile spellings.

    Root files carry ``:root``, never the bare token: the refusal names the token to
    file, and a bare ``hermes-config`` covers every lane's config.yaml.
    """
    if rel == ("config.yaml",):
        return SurfaceHit(
            surface="hermes-config",
            scope_token=f"hermes-config:{_ROOT_TOKEN}",
            path=path_text,
            reason="write to the root Hermes config.yaml",
        )
    if rel == (".env",):
        return SurfaceHit(
            surface="hermes-env",
            scope_token=f"hermes-env:{_ROOT_TOKEN}",
            path=path_text,
            reason="write to the root Hermes .env",
        )
    if rel == ("SOUL.md",):
        # The root home is a live seat's home: agent/prompt_builder.py loads
        # get_hermes_home()/"SOUL.md" as identity slot #1, so this is a persona
        # write, not a stray markdown file.
        return SurfaceHit(
            surface="hermes-soul",
            scope_token=f"hermes-soul:{_ROOT_TOKEN}",
            path=path_text,
            reason="write to the root Hermes SOUL.md (the live seat's persona)",
        )
    if len(rel) == 3 and rel[0] == "profiles":
        profile, leaf = rel[1], rel[2]
        if leaf == "config.yaml":
            return SurfaceHit(
                surface="hermes-config",
                scope_token=f"hermes-config:{profile}",
                path=path_text,
                reason=f"write to profile {profile!r} config.yaml",
            )
        if leaf == ".env":
            return SurfaceHit(
                surface="hermes-env",
                scope_token=f"hermes-env:{profile}",
                path=path_text,
                reason=f"write to profile {profile!r} .env",
            )
        if leaf == "SOUL.md":
            return SurfaceHit(
                surface="hermes-soul",
                scope_token=f"hermes-soul:{profile}",
                path=path_text,
                reason=f"write to profile {profile!r} SOUL.md",
            )
        return None
    if rel and rel[0] == "plugins":
        return _plugin_hit(rel, path_text)
    return None


def _launchd_hit(segs: tuple[str, ...], path_text: str) -> SurfaceHit | None:
    if len(segs) < 3 or segs[-3] != "Library" or segs[-2] != "LaunchAgents":
        return None
    leaf = segs[-1]
    if not leaf.endswith(".plist") or leaf == ".plist":
        return None
    return SurfaceHit(
        surface="launchd-plist",
        scope_token=f"launchd-plist:{leaf}",
        path=path_text,
        reason=f"write to the launchd LaunchAgent plist {leaf!r}",
    )


# --- the path classifier ----------------------------------------------------


def classify_path(
    path: str,
    *,
    hermes_home: str | None = None,
    install_root: str | None = None,
) -> SurfaceHit | None:
    """Return the surface a write to ``path`` would touch, or ``None``.

    Pure: no filesystem, env or DB read.  Callers should pass an absolute,
    symlink-resolved path -- resolution is theirs to do (see the module docstring).
    """
    if path is None:
        return None
    path_text = _strip_quotes(str(path))
    if not path_text:
        return None
    normalised = _normalise(path_text)
    if not normalised:
        return None
    segs = _segments(normalised)
    if not segs:
        return None

    hit = _launchd_hit(segs, path_text)
    if hit is not None:
        return hit

    rel = _hermes_relative(segs, hermes_home)
    if rel:
        hit = _hermes_relative_hit(rel, path_text)
        if hit is not None:
            return hit

    rel = _install_relative(segs, install_root)
    if rel and rel[0] == "plugins":
        hit = _plugin_hit(rel, path_text)
        if hit is not None:
            return hit
    return None


# --- the command classifier -------------------------------------------------

_REDIRECT_RE = re.compile(r">{1,2}\|?\s*(\"[^\"]*\"|'[^']*'|[^\s;&|()<>]+)")
_SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "2>", "2>>"})
_CONFIG_MUTATIONS = frozenset({"set", "unset", "edit"})
_HELP_TOKENS = frozenset({"-h", "--help", "help"})
# Global flags that take their value as a SEPARATE token.  Without this table the
# value of a space-separated flag is read as the command word -- and ``hermes -p``
# is the host's normal profile selector, so that spelling is reachable, not exotic.
_GLOBAL_VALUE_FLAGS = frozenset({"-p", "--profile"})
_COPY_COMMANDS = frozenset({"cp", "mv", "install"})
_INPLACE_TOOLS = frozenset({"sed", "perl", "ruby"})
_SHARED_SURFACE_KEYS = frozenset(
    {
        "overwrite system file via redirection",
        "overwrite system file via tee",
        "in-place edit of Hermes config/env",
        "in-place edit of Hermes config/env (long flag)",
        "in-place edit of Hermes config/env (perl/ruby)",
    }
)

_shared_detector = None  # lazy: tools.approval_detection.detect_dangerous_command
_shared_detector_tried = False


def _get_shared_detector():
    """The terminal layer's own detector, or ``None`` when it is not importable."""
    global _shared_detector, _shared_detector_tried
    if not _shared_detector_tried:
        _shared_detector_tried = True
        try:
            from tools.approval_detection import detect_dangerous_command
        except (ImportError, AttributeError):  # standalone / vendored use
            _shared_detector = None
        else:
            _shared_detector = detect_dangerous_command
    return _shared_detector


def _shared_surface_key(text: str) -> str | None:
    detector = _get_shared_detector()
    if detector is None:
        return None
    try:
        dangerous, key, _detail = detector(text)
    except Exception:  # health: allow BLE001 -- the detector is the terminal layer's code; a crash there must not turn a surface write into a classifier error. The local write-shape tokeniser below still covers the class, so this degrades to the pre-reuse answer instead of failing open.
        return None
    if dangerous and key in _SHARED_SURFACE_KEYS:
        return key
    return None


def _tokens(argv) -> list[str]:
    if isinstance(argv, (list, tuple)):
        return [_strip_quotes(str(tok)) for tok in argv if str(tok).strip()]
    text = "" if argv is None else str(argv)
    try:
        return shlex.split(text)
    except ValueError:  # unbalanced quotes: fall back to a dumb split
        return text.split()


def _positionals(tokens: list[str]) -> list[str]:
    return [
        tok
        for tok in tokens
        if tok not in _SHELL_OPERATORS and not tok.startswith("-") and tok
    ]


def _redirect_targets(text: str) -> list[str]:
    return [_strip_quotes(match.group(1)) for match in _REDIRECT_RE.finditer(text)]


def _tee_targets(tokens: list[str]) -> list[str]:
    targets: list[str] = []
    for idx, tok in enumerate(tokens):
        if posixpath.basename(_strip_quotes(tok)) != "tee":
            continue
        for nxt in tokens[idx + 1 :]:
            if nxt in _SHELL_OPERATORS:
                break
            if nxt.startswith("-"):
                continue
            targets.append(_strip_quotes(nxt))
    return targets


def _inplace_shape(tokens: list[str]) -> bool:
    """``sed -i``/``perl -i``/``ruby -i`` -- the in-place write SHAPE.

    The shared corpus covers this for the root config/env spellings only; the
    shape itself is what makes a SOUL.md/plugins/plist in-place edit a write.
    """
    for idx, tok in enumerate(tokens):
        base = posixpath.basename(_strip_quotes(tok))
        if base not in _INPLACE_TOOLS:
            continue
        for flag in tokens[idx + 1 :]:
            if flag.startswith("--in-place"):
                return True
            if flag.startswith("-") and not flag.startswith("--") and "i" in flag:
                return True
            if not flag.startswith("-"):
                break
    return False


def _drop_global_flag_values(tokens: list[str]) -> list[str]:
    """Remove the value token of each space-separated global flag (``-p x``)."""
    kept: list[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok in _GLOBAL_VALUE_FLAGS:
            idx += 2
            continue
        kept.append(tok)
        idx += 1
    return kept


def _cli_config_hit(tokens: list[str]) -> SurfaceHit | None:
    """``hermes config set|unset|edit`` -- the argv surface (D1 row 6)."""
    hermes_at = None
    for idx, tok in enumerate(tokens):
        if posixpath.basename(_strip_quotes(tok)) == "hermes":
            hermes_at = idx
            break
    if hermes_at is None:
        return None

    # Strip ``-p <profile>``/``--profile <profile>`` values once, up front: they
    # shift every later position scan and the dotted-key scan would otherwise read
    # the profile name as the key.
    remaining = _drop_global_flag_values(tokens[hermes_at + 1 :])
    command_at = None
    for idx, tok in enumerate(remaining):
        if tok.startswith("-"):
            continue
        command_at = idx
        break
    if command_at is None or remaining[command_at] != "config":
        return None

    tail = remaining[command_at + 1 :]
    sub_at = None
    for idx, tok in enumerate(tail):
        if tok.startswith("-"):
            continue
        sub_at = idx
        break
    if sub_at is None:
        return None
    subcommand = tail[sub_at].lower()
    if subcommand not in _CONFIG_MUTATIONS:
        return None

    args = tail[sub_at + 1 :]
    if any(arg in _HELP_TOKENS for arg in args):
        return None
    key = None
    if subcommand != "edit":
        for arg in args:
            if arg.startswith("-"):
                continue
            key = _strip_quotes(arg)
            break

    token = f"hermes-config:{key}" if key else "hermes-config"
    described = f"hermes config {subcommand}" + (f" {key}" if key else "")
    return SurfaceHit(
        surface="hermes-cli-config",
        scope_token=token,
        path=None,
        reason=f"{described} rewrites the live config without a covering approval",
    )


def _copy_destination(tokens: list[str]) -> str | None:
    """Destination of ``cp``/``mv``/``install`` -- the last positional argument."""
    for idx, tok in enumerate(tokens):
        if posixpath.basename(_strip_quotes(tok)) not in _COPY_COMMANDS:
            continue
        rest = _positionals(tokens[idx + 1 :])
        if len(rest) >= 2:
            return rest[-1]
        return None
    return None


def classify_command(
    argv,
    text: str | None = None,
    *,
    hermes_home: str | None = None,
    install_root: str | None = None,
) -> SurfaceHit | None:
    """Return the surface a command would write, or ``None``.

    ``argv`` is the tokenised command (a sequence of strings, or a command string
    that is split here); ``text`` is the verbatim command as issued, where the
    redirect / tee targets live.  Reads return ``None``: the write SHAPE has to be
    present before any path in the command is treated as a destination, which is
    what keeps ``cat ~/.hermes/.env`` and ``grep config.yaml`` out of the class.
    """
    tokens = _tokens(argv if text is None else text)
    command_text = " ".join(tokens) if text is None else str(text)

    hit = _cli_config_hit(tokens)
    if hit is not None:
        return hit

    # Targets of the write SHAPE only: an unredirected read of a surface path
    # (``cat ~/.hermes/.env``) and a surface used as a copy SOURCE must stay out.
    targets = _redirect_targets(command_text) + _tee_targets(tokens)
    in_place = bool(_shared_surface_key(command_text)) or _inplace_shape(tokens)
    if in_place:
        # ``sed -i``/``perl -i`` name their target as the last positional.
        targets += _positionals(tokens)[-1:]
    for target in targets:
        hit = classify_path(target, hermes_home=hermes_home, install_root=install_root)
        if hit is not None:
            return hit

    destination = _copy_destination(tokens)
    if destination is not None:
        hit = classify_path(
            destination, hermes_home=hermes_home, install_root=install_root
        )
        if hit is not None:
            return hit
    return None
