"""Detect a recursive filesystem search that nothing bounds — ``find / -name x``,
``grep -rl pat ~``, a ``grep -rl pat .`` whose cwd is the Hermes home.

Why this exists: a recursive walk rooted at the filesystem root or at a whole home
directory reads the entire tree. One such ``grep -r`` held a full core for 12m32s on
this host and returned nothing a depth-bounded search would not have returned in
seconds; re-issued from a worker started with ``cwd=~/.hermes``, a ``grep -rl pat .``
walks ~100 GB of sessions, logs and workspaces. The host is the shared substrate every
lane runs on, so the walk is refused at the tool boundary and the model is handed the
bounded form instead.

What counts as unbounded — the check is keyed on the *walk* and the *root*, never on
the executable alone:

* a recursive invocation (``find`` and ``rg`` walk by default; ``grep`` needs
  ``-r``/``-R``/``--recursive``/``-d recurse``), with no bound in argv
  (``find -maxdepth``, ``rg --max-depth``; ``grep`` has no depth bound at all, so only
  a narrower root helps), and
* a root that is a whole home directory, the Hermes home, a lane profile home
  (``~/.hermes/profiles/<lane>`` — where a worker is usually started, so ``find .`` and
  ``find ~/.hermes/profiles/<lane>`` are the same walk), ``/``, a container of home and
  repo trees (``/Users``, ``/home``, ``/opt``), or ``.``/``..``/no root at all resolving
  to one of those.

A named subtree — a project, a workspace, a directory inside the Hermes home such as
``~/.hermes/kanban`` — is never broad, however deep it sits. So ``grep -rn pat
~/.hermes/skills``, ``rg -n foo tools/`` and ``find <dir> -maxdepth 3 -name '*.py'``
still run: false positives here would cost more than the burn they prevent.

Deliberately string/argv-only: no filesystem walk, no script reading, nothing that can
block. A search hidden inside an *executed script file* is out of scope (the gateway
guard reads referenced scripts; this one does not, by design). ``ls -R``, ``du -a`` and
``fd`` are likewise out of scope today — the spec table is where one more tool goes.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Tuple

from cron.lifecycle_guard import (
    _executable_name,
    _executed_command_index,
    _expand_candidate_path,
    _iter_command_segments,
    _iter_shell_command_payloads,
    _resolve_lenient,
)

logger = logging.getLogger("tools.terminal_tool")

# ``sh -c '<search>'`` and friends: rescanning payloads is cheap string work, but the
# nesting is capped so a self-referential command cannot spin this guard.
_MAX_PAYLOAD_DEPTH = 3

# Searching this is what the guard exists to stop: it holds a whole home, or the tree
# that holds the homes. A path *below* any of them is a named subtree, never broad.
_BROAD_CONTAINER_DIRECTORIES = ("/Users", "/home", "/opt")
_HOME_DIRECTORY_PARENTS = ("Users", "home")

# Shell-expanded spellings shlex hands us literally.
_HOME_TOKEN_ROOTS = ("$HOME", "${HOME}")

# ``find --help`` must not be read as a walk of ``.``.
_HELP_ONLY_OPTIONS = frozenset({"--help", "--version"})


class _SearchTool(NamedTuple):
    """How one recursive-search executable spells a walk and where its roots sit.

    ``value_options`` are options that consume their value as the next token
    (``-m 1``) or as an attached one (``-m1``, ``--include=x``); ``pattern_options``
    are the ones that supply the search pattern, which is what tells us whether the
    first positional operand is the pattern or already a root.
    """

    recursive_flags: frozenset = frozenset()
    recursive_flag_letters: frozenset = frozenset()
    recursive_value_options: Dict[str, str] = {}
    recursive_by_default: bool = False
    bounded_options: frozenset = frozenset()
    value_options: frozenset = frozenset()
    pattern_options: frozenset = frozenset()
    pattern_operand: bool = False
    patternless_options: frozenset = frozenset()
    roots_lead_expression: bool = False


_SEARCH_TOOLS: Dict[str, _SearchTool] = {
    # find PATH... [expression]: the path list ends at the first expression token.
    "find": _SearchTool(
        recursive_by_default=True,
        bounded_options=frozenset({"-maxdepth", "--maxdepth", "--max-depth"}),
        roots_lead_expression=True,
    ),
    "grep": _SearchTool(
        recursive_flags=frozenset({"-r", "-R", "--recursive", "--dereference-recursive"}),
        recursive_flag_letters=frozenset({"r", "R"}),
        recursive_value_options={"-d": "recurse", "--directories": "recurse"},
        value_options=frozenset({
            "-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D",
            "--regexp", "--file", "--max-count", "--after-context", "--before-context",
            "--context", "--include", "--exclude", "--exclude-dir", "--include-dir",
            "--exclude-from", "--label", "--binary-files", "--devices", "--directories",
        }),
        pattern_options=frozenset({"-e", "-f", "--regexp", "--file"}),
        pattern_operand=True,
    ),
    # ripgrep: recursive by default, and its ``-r`` is --replace (a value option), not
    # a recursion flag — so short-flag clustering must not read ``-r`` here.
    "rg": _SearchTool(
        recursive_by_default=True,
        bounded_options=frozenset({"--max-depth"}),
        value_options=frozenset({
            "-e", "-f", "-g", "-t", "-T", "-m", "-A", "-B", "-C", "-r", "-E", "-j", "-M",
            "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--type-add",
            "--max-count", "--after-context", "--before-context", "--context", "--replace",
            "--encoding", "--engine", "--threads", "--max-depth", "--max-columns",
            "--max-filesize", "--sort", "--sortr", "--field-match-separator",
            "--field-context-separator", "--context-separator", "--colors",
        }),
        pattern_options=frozenset({"-e", "-f", "--regexp", "--file"}),
        pattern_operand=True,
        patternless_options=frozenset({"--files"}),
    ),
}


class SearchInvocation(NamedTuple):
    """A search a command segment runs: its tool, its roots, whether it recurses, whether the walk is bounded."""

    tool: str
    roots: List[str]
    recursive: bool
    bounded: bool


def _option_head(token: str) -> str:
    """Option name in *token*: ``--include=x`` -> ``--include``, ``-rl`` -> ``-rl``."""
    return token.partition("=")[0]


def _clusters_a_recursive_flag(token: str, letters: frozenset) -> bool:
    """True for a bundled short-flag token (``-rl``) carrying a recursion letter."""
    return (
        token.startswith("-")
        and not token.startswith("--")
        and token[1:].isalpha()
        and bool(set(token[1:]) & letters)
    )


def _search_invocation(segment: List[str]) -> Optional[SearchInvocation]:
    """The recursive search *segment* runs, or None when it runs none.

    Returns the invocation for any ``find``/``grep``/``rg`` call, recursive or not; the
    caller decides, so the recursion test lives in one place.
    """
    index = _executed_command_index(segment)
    if index is None:
        return None
    tool = _executable_name(segment[index])
    spec = _SEARCH_TOOLS.get(tool)
    if spec is None:
        return None
    tokens = segment[index + 1:]
    if _HELP_ONLY_OPTIONS.intersection(tokens):
        return None
    if any(_option_head(token) in spec.bounded_options for token in tokens):
        return SearchInvocation(tool=tool, roots=[], recursive=spec.recursive_by_default, bounded=True)

    recursive = spec.recursive_by_default
    pattern_supplied = False
    patternless = False
    operands: List[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":  # POSIX end-of-options: everything after names a root.
            operands.extend(tokens[index + 1:])
            break
        if spec.roots_lead_expression and token.startswith(("-", "!", "(")):
            break  # find: the path list ended, this is the expression
        name, separator, attached = token.partition("=")
        if name in spec.patternless_options:
            patternless = True
        if name in spec.recursive_flags or _clusters_a_recursive_flag(token, spec.recursive_flag_letters):
            recursive = True
        if name in spec.value_options:
            takes_next = not separator and index + 1 < len(tokens)
            value = attached if not takes_next else tokens[index + 1]
            if spec.recursive_value_options.get(name) == value:
                recursive = True
            pattern_supplied = pattern_supplied or name in spec.pattern_options
            index += 2 if takes_next else 1
            continue
        if token.startswith("-") and token != "-":
            pattern_supplied = pattern_supplied or token[:2] in spec.pattern_options
            index += 1
            continue
        operands.append(token)
        index += 1

    if spec.pattern_operand and not pattern_supplied and not patternless and operands:
        operands = operands[1:]  # first positional operand is the pattern, not a root
    return SearchInvocation(tool=tool, roots=operands, recursive=recursive, bounded=False)


def _hermes_home_directories() -> frozenset:
    """The Hermes homes a worker may be sitting in: the active one and the shared ``~/.hermes``."""
    from hermes_constants import get_hermes_home

    try:
        homes = {_resolve_lenient(get_hermes_home()), _resolve_lenient(Path.home() / ".hermes")}
    except (OSError, RuntimeError):  # no resolvable HOME (launchd) — the token check still runs
        return frozenset()
    return frozenset(homes)


def _home_token_path(token: str) -> Optional[str]:
    """``$HOME``/``${HOME}``/``$HERMES_HOME`` as a real path; None when the token is something else."""
    from hermes_constants import get_hermes_home

    if token in _HOME_TOKEN_ROOTS:
        try:
            return str(Path.home())
        except (OSError, RuntimeError):
            return None
    if token in ("$HERMES_HOME", "${HERMES_HOME}"):
        try:
            return str(get_hermes_home())
        except (OSError, RuntimeError):
            return None
    return None


def _broad_search_root_label(root: str, cwd: str) -> Optional[str]:
    """Label naming *root* when recursively searching all of it is unbounded, else None."""
    expanded = _home_token_path(root) or root
    candidate = _expand_candidate_path(expanded)
    if candidate is None:
        return None
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate  # ``.``, ``..`` and relative roots are cwd-anchored
    resolved = _resolve_lenient(candidate)
    for home in _hermes_home_directories():
        if resolved == home:
            return f"the Hermes home ({resolved})"
        # Each lane's home holds its own sessions, logs and workspaces; workers are
        # started with cwd set to one of these, so ``find .`` here is the same walk.
        if resolved == home / "profiles" or resolved.parent == home / "profiles":
            return f"a lane profile home ({resolved})"
    if resolved.parent == resolved:
        return "the filesystem root"
    if len(resolved.parts) == 3 and resolved.parts[1] in _HOME_DIRECTORY_PARENTS:
        return f"a whole home directory ({resolved})"
    if str(resolved) in _BROAD_CONTAINER_DIRECTORIES:
        return f"{resolved}, which contains whole home and repo trees"
    return None


def unbounded_search_root(invocation: SearchInvocation, cwd: str) -> Optional[str]:
    """Label of the first root that makes *invocation* an unbounded walk, else None.

    No root at all means every one of these tools searches ``.``.
    """
    if not invocation.recursive or invocation.bounded:
        return None
    for root in invocation.roots or (".",):
        label = _broad_search_root_label(root, cwd)
        if label:
            return label
    return None


def _scan_segments(command: str, depth: int = 0) -> Iterator[List[str]]:
    """Segments to inspect: the command's own, then any ``sh -c`` payload, depth-capped."""
    yield from _iter_command_segments(command)
    if depth >= _MAX_PAYLOAD_DEPTH:
        return
    for payload in _iter_shell_command_payloads(command):
        yield from _scan_segments(payload, depth + 1)


def scan_unbounded_search(command: str, cwd: str) -> Optional[Tuple[str, str]]:
    """``(tool, root_label)`` for the first unbounded recursive search in *command*, else None."""
    for segment in _scan_segments(command):
        invocation = _search_invocation(segment)
        if invocation is None:
            continue
        label = unbounded_search_root(invocation, cwd)
        if label:
            return invocation.tool, label
    return None


def unbounded_search_message(tool: str, root_label: str) -> str:
    """The refusal: what was refused, why it costs, and the bounded forms that pass."""
    return (
        f"Blocked: this `{tool}` walks {root_label} recursively with nothing bounding the walk "
        "(no `-maxdepth`/`--max-depth`, and no narrower root). That is the class of command that "
        "held a full core for 12m32s on this host and returned nothing a bounded search would not. "
        "Re-issue it bounded: name the subtree you actually need "
        "(`grep -rl -m1 --include='*.md' PAT ~/.hermes/kanban`), bound the depth "
        "(`find <dir> -maxdepth 3 -name '<x>'`), or use the known path. A search rooted at a "
        "project, workspace or named subtree — `find`, `grep` or `rg` in any form — is unaffected."
    )


def resolve_search_guard_cwd(
    *, env: Any, env_type: str, cwd: str, workdir: Optional[str], session_key: str
) -> str:
    """cwd the command will actually run in — the root a ``.``-anchored search resolves against."""
    from tools.terminal_tool import _resolve_command_cwd, get_session_cwd

    base = get_session_cwd(session_key)
    if base is None:
        base = getattr(env, "cwd", None) or cwd
    return _resolve_command_cwd(
        workdir=workdir, default_cwd=base, session_key=session_key, env_type=env_type,
    )
