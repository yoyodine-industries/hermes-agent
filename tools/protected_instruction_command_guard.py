"""Write-target extraction for the COMMAND tools (terminal + execute_code).

``tools/file_tools_write_guards.py`` gates the FILE-write vector: a path that lands on an
agent-instruction file (``SOUL.md`` / ``AGENTS.md`` / ``CLAUDE.md``, a
``security.protected_instruction_extra_patterns`` hit, a project-local
``.hermes/config.yaml``) needs one-operation human approval, every time.

The same file is reachable without write_file/patch — ``echo x > SOUL.md``,
``python3 -c "open('SOUL.md','w')"``, ``tee``, ``shutil.copy`` inside execute_code — so
the gate has to run at the dispatch layer too, or the file gate is decoration.

This module owns EXTRACTION only: which literals in a command line or a Python snippet
are WRITE targets. Matching stays in ``file_tools_write_guards`` and is CALLED from here
(``_check_protected_instruction_write``), so a config key, an allowlist dir, the
``~/.hermes`` exemption or an identity pattern can never mean one thing for write_file
and another for the shell — the drift that would make one vector a bypass.

Best effort by construction: the scan reads the TEXT of the call, so a path that only
exists at runtime (``F=SOUL.md; cp x $F``, ``exec`` of computed source, a base64 payload)
is out of reach. What it does guarantee is the shapes a model reaches for, without a
false positive on an ordinary READ (``cat SOUL.md``, ``grep -n x AGENTS.md``) — blocking
those would make reading project files impossible.
"""

import ast
import logging
import re
import shlex
from typing import Optional

from tools.registry import tool_error
from tools.shell_heredoc import _find_heredoc_close, _parse_heredoc_operator
from tools.terminal_tool_guards import _blocked_json

logger = logging.getLogger("tools.terminal_tool")

TOOL_TERMINAL = "terminal"
TOOL_EXECUTE_CODE = "execute_code"

# Depth cap for interpreter nesting (``sh -c "python3 -c '...'"``, heredoc feeding python).
_MAX_DEPTH = 3


def write_targets(text: str, *, tool: str = TOOL_TERMINAL) -> list[str]:
    """Path literals in *text* that the call would WRITE, deduped and in order.

    ``tool`` picks the language of *text*: an execute_code cell is Python source, a
    terminal call is a shell command line (which may embed interpreters).
    """
    language = "python" if tool == TOOL_EXECUTE_CODE else "shell"
    return list(dict.fromkeys(_scan(text, language=language, depth=0)))


def protected_instruction_block(*, text: str, tool: str, task_id: str = "default") -> Optional[str]:
    """Finished blocked-result string for *tool* when *text* writes a protected
    instruction file, else ``None``.

    Matching AND the approval prompt are the file tools' own
    (``_check_sensitive_path`` hard-deny for the Hermes config.yaml and system paths,
    then ``_check_protected_instruction_write`` -> one-operation approval, no persisted
    scope, fail-closed without a human channel), in the file tools' order, so a terminal
    command or an execute_code cell faces exactly the chain write_file/patch faces.
    Callers must run this OUTSIDE any ``force``/auto-approve branch: the gate is not
    bypassable.
    """
    try:
        from tools.file_tools_write_guards import (
            _check_protected_instruction_write, _check_sensitive_path,
        )
    except Exception:  # pragma: no cover - import failure, same one the file tool hits
        logger.warning("Protected-instruction guard unavailable; %s write not gated", tool)
        return None
    targets = write_targets(text, tool=tool)
    if not targets:
        return None
    # Same order as the file tools: hard denies first (no prompt, no escape hatch), then
    # the approval gate. The hard-deny branch is a deliberate behavior change for the
    # command vector: ``write_file`` could never touch the Hermes config.yaml or /etc, and
    # a refusal the shell could walk around is not a refusal.
    for target in targets:
        verdict = _check_sensitive_path(target, task_id)
        if verdict:
            logger.warning("Blocked %s write to sensitive path: %s", tool, target)
            return _blocked_result(verdict, tool=tool)
    verdict = _check_protected_instruction_write(targets, task_id)
    if verdict is None:  # the human approved (or nothing was actually protected)
        return None
    logger.warning("Blocked %s write to protected instruction file(s): %s", tool, ", ".join(targets))
    return _blocked_result(f"{verdict} The {tool} call was refused before it ran.", tool=tool)


def _blocked_result(message: str, *, tool: str) -> str:
    """The blocked envelope for *tool* (terminal: output/exit_code/error/status JSON)."""
    return _blocked_json(message, "blocked") if tool == TOOL_TERMINAL else tool_error(message)


# ---- scanning ---------------------------------------------------------------------


def _scan(text: str, *, language: str, depth: int) -> list[str]:
    """Write targets in *text*, recursing into embedded interpreter/shell payloads."""
    if not text or depth > _MAX_DEPTH:
        return []
    if language == "shell":
        return _shell_write_targets(text) + _interpreter_payload_targets(text, depth) + \
            _heredoc_payload_targets(text, depth)
    tree = _parse_python(text) if language == "python" else None
    if tree is not None:
        return _python_tree_targets(tree, depth)
    # Unparseable Python (a fragment) or another language's ``-e`` payload: the
    # regex shapes for the code we cannot parse, plus shell redirects it may carry.
    return _regex_code_targets(text) + _shell_write_targets(text)


def _parse_python(text: str) -> Optional[ast.AST]:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


# ---- shell ------------------------------------------------------------------------

_SHELL_PUNCTUATION = "<>|&;()"
_SHELL_OPERATORS = frozenset({
    "|", "||", "&", "&&", ";", ";;", "(", ")", "{", "}", "<", "<<", "<<<", ">", ">>", ">|", "&>", "&>>",
})
# ``>&2``/``<&3`` duplicate a descriptor; ``2>``/``&>`` open a file. Anything the shell
# commander tokenizes into the -duplicate form is skipped, so ``2>&1`` yields no target.
_FD_DUPLICATE_OPS = frozenset({">&", "<&", "&<"})
_WRITE_REDIRECT_OPS = frozenset({">", ">>", ">|", "&>", "&>>", "1>", "1>>", "2>", "2>>"})

# Commands whose non-flag operands name a file being written. The second element is the
# flags that consume the NEXT argument (so their value is not mistaken for a path).
#   "last"     -> the final operand is the destination (cp/mv/install/rsync/scp/ln)
#   "all"      -> every operand is written (tee/sponge/truncate)
#   "of="      -> dd's ``of=FILE``
#   "flag"     -> the value of an output flag (curl/wget/sort)
#   "in-place" -> sed rewriting its file operands
_WRITE_VERBS = {
    "cp": ("last", ()), "mv": ("last", ()), "install": ("last", ()), "rsync": ("last", ()),
    "scp": ("last", ()), "ln": ("last", ()), "dd": ("of=", ()),
    "tee": ("all", ()), "sponge": ("all", ()), "truncate": ("all", ("-s", "-r", "--size", "--reference")),
    "curl": ("flag", ("-o", "--output")), "wget": ("flag", ("-O", "--output-document")),
    "sort": ("flag", ("-o", "--output")), "sed": ("in-place", ()),
}

# Words that may precede a command without making the verb an argument of another command.
_COMMAND_PREFIXES = frozenset({
    "sudo", "doas", "env", "command", "builtin", "nice", "ionice", "time", "nohup", "setsid", "exec",
})


def _shell_tokens(command: str) -> list[str]:
    """Split *command* into words and operators, with quotes resolved.

    ``shlex`` in posix mode is the only tokenizer here that keeps a quoted path whole
    (``echo x > "my SOUL.md"``) while exposing ``>``/``2>``/``|`` as separate tokens.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to whitespace, still redirection-visible
        return command.split()


def _shell_write_targets(command: str) -> list[str]:
    tokens = _shell_tokens(command)
    targets = _redirect_targets(tokens)
    for index, token in enumerate(tokens):
        verb = token.rsplit("/", 1)[-1]
        if verb not in _WRITE_VERBS or not _is_command_start(tokens, index):
            continue
        targets.extend(_verb_targets(verb, _command_args(tokens, index)))
    return targets


def _redirect_targets(tokens: list[str]) -> list[str]:
    targets: list[str] = []
    for index, token in enumerate(tokens):
        if token not in _WRITE_REDIRECT_OPS:
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        if not following or following in _SHELL_OPERATORS or following.startswith(("&", ">")):
            continue  # descriptor duplication (``>&2``) or a bare operator
        targets.append(following)
    return targets


def _is_command_start(tokens: list[str], index: int) -> bool:
    """True when token *index* opens a command (so ``echo cp x`` is not a ``cp`` call)."""
    while index > 0:
        previous = tokens[index - 1]
        if previous in _SHELL_OPERATORS:
            return True
        if previous.rsplit("/", 1)[-1] in _COMMAND_PREFIXES or (previous.startswith("-") and len(previous) > 1):
            index -= 1
            continue
        return False
    return True


def _command_args(tokens: list[str], verb_index: int) -> list[str]:
    """Args of the command at *verb_index*, stopping at the next shell operator."""
    args: list[str] = []
    for token in tokens[verb_index + 1:]:
        if token in _SHELL_OPERATORS:
            break
        args.append(token)
    return args


def _verb_targets(verb: str, args: list[str]) -> list[str]:
    mode, value_flags = _WRITE_VERBS[verb]
    if mode == "of=":
        return [arg.split("=", 1)[1] for arg in args if arg.startswith("of=") and len(arg) > 3]
    if mode == "flag":
        return _flag_value_targets(args, value_flags)
    if mode == "in-place":
        if not _has_in_place_flag(args):
            return []
        operands = _operands(args, value_flags=("-e", "-f", "--expression", "--file"))
        if not _has_script_flag(args):
            operands = operands[1:]  # the first operand is the sed SCRIPT, not a file
        return operands
    operands = _operands(args, value_flags=value_flags)
    return operands[-1:] if mode == "last" else operands


def _operands(args: list[str], *, value_flags: tuple[str, ...] = ()) -> list[str]:
    """Non-flag args, minus the values consumed by *value_flags*."""
    operands: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg in value_flags:
            skip_value = True
            continue
        if any(arg.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
            continue
        if arg.startswith("-") and arg != "-":
            continue
        operands.append(arg)
    return operands


def _flag_value_targets(args: list[str], flags: tuple[str, ...]) -> list[str]:
    targets: list[str] = []
    for index, arg in enumerate(args):
        for flag in flags:
            if arg == flag and index + 1 < len(args):
                targets.append(args[index + 1])
            elif flag.startswith("--") and arg.startswith(flag + "="):
                targets.append(arg[len(flag) + 1:])
    return targets


def _has_in_place_flag(args: list[str]) -> bool:
    return any(arg == "-i" or arg.startswith("-i.") or arg.startswith("--in-place") for arg in args)


def _has_script_flag(args: list[str]) -> bool:
    return any(arg in ("-e", "-f") or arg.startswith(("--expression=", "--file=")) for arg in args)


# ---- interpreter payloads ---------------------------------------------------------

_INTERPRETER_LANGUAGES = {
    "sh": "shell", "bash": "shell", "zsh": "shell", "dash": "shell", "ksh": "shell",
    "python": "python", "perl": "generic", "ruby": "generic", "node": "generic", "php": "generic",
}
_CODE_FLAGS = ("-c", "-e", "-E", "-p", "--eval", "--execute")


def _interpreter_language(text: str) -> str:
    """Language of the interpreter *text* invokes, or "" when it invokes none."""
    for token in _shell_tokens(text):
        name = token.rsplit("/", 1)[-1]
        if name.startswith("python"):
            return "python"
        language = _INTERPRETER_LANGUAGES.get(name)
        if language:
            return language
    return ""


def _interpreter_payload_targets(command: str, depth: int) -> list[str]:
    """``python3 -c "open('SOUL.md','w')"`` and friends: scan the inline payload."""
    tokens = _shell_tokens(command)
    targets: list[str] = []
    for index, token in enumerate(tokens):
        language = _interpreter_language(token)
        if not language or not _is_command_start(tokens, index):
            continue
        args = _command_args(tokens, index)
        for position, arg in enumerate(args):
            if arg in _CODE_FLAGS and position + 1 < len(args):
                targets.extend(_scan(args[position + 1], language=language, depth=depth + 1))
                break
    return targets


def _heredoc_payload_targets(command: str, depth: int) -> list[str]:
    """A heredoc fed to an interpreter is source code: ``python3 - <<'PY'`` ... ``PY``."""
    targets: list[str] = []
    cursor = command.find("<<")
    while cursor != -1:
        parsed = _parse_heredoc_operator(command, cursor)
        if parsed is None:
            cursor = command.find("<<", cursor + 2)
            continue
        operator_end, delimiter, strip_tabs, _quoted = parsed
        newline = command.find("\n", operator_end)
        if newline == -1:
            break
        body_start = newline + 1
        close = _find_heredoc_close(command, body_start, delimiter, strip_tabs)
        if close is None:
            break
        language = _interpreter_language(_command_unit_before(command, cursor))
        if language:
            targets.extend(_scan(command[body_start:close], language=language, depth=depth + 1))
        cursor = close
    return targets


def _command_unit_before(command: str, index: int) -> str:
    """Text of the command unit the ``<<`` at *index* belongs to (its consumer)."""
    unit_start = max(command.rfind(sep, 0, index) for sep in ("\n", ";", "|", "&")) + 1
    return command[unit_start:index]


# ---- python -----------------------------------------------------------------------

# Python calls whose ARG at the given position is a written path, with the rule that
# decides whether the call writes at all ("any" / "write-mode" / "write-flags").
_PATH_ARG_CALLS = {
    "write_file": (0, "any"), "patch": (0, "any"),
    "open": (0, "write-mode"), "io.open": (0, "write-mode"), "codecs.open": (0, "write-mode"),
    "os.open": (0, "write-flags"),
    "os.rename": (1, "any"), "os.replace": (1, "any"), "os.link": (1, "any"), "os.symlink": (1, "any"),
    "shutil.copy": (1, "any"), "shutil.copy2": (1, "any"), "shutil.copyfile": (1, "any"),
    "shutil.move": (1, "any"), "shutil.copytree": (1, "any"),
}
# Path-object methods that write, keyed by the attribute name.
_PATH_METHODS = {"write_text": "any", "write_bytes": "any", "open": "write-mode"}
_SHELL_LAUNCHER_CALLS = frozenset({
    "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "subprocess.Popen", "os.system", "os.popen", "os.execv", "os.execvp", "os.spawnv",
})
_WRITE_MODE_CHARS = frozenset("wax+")
_WRITE_FLAG_NAMES = frozenset({"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"})


def _python_tree_targets(tree: ast.AST, depth: int) -> list[str]:
    targets: list[str] = []
    shell_payloads: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        if name in _SHELL_LAUNCHER_CALLS:
            shell_payloads.extend(_shell_payloads(node.args))
            continue
        targets.extend(_write_call_targets(name, node))
    for payload in shell_payloads:
        targets.extend(_scan(payload, language="shell", depth=depth + 1))
    return targets


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    return ""


def _write_call_targets(name: str, node: ast.Call) -> list[str]:
    spec = _PATH_ARG_CALLS.get(name)
    if spec is not None:
        position, rule = spec
        if _call_writes(name, rule, node) and len(node.args) > position:
            return _literal_paths(node.args[position])
        return []
    attribute = name.rsplit(".", 1)[-1]
    rule = _PATH_METHODS.get(attribute)
    if rule is not None and isinstance(node.func, ast.Attribute) and _call_writes(attribute, rule, node):
        return _literal_paths(node.func.value)
    return []


def _call_writes(name: str, rule: str, node: ast.Call) -> bool:
    if rule == "any":
        return True
    if rule == "write-mode":
        return _opens_for_write(name, node)
    return _opens_with_write_flags(node)


def _opens_for_write(name: str, node: ast.Call) -> bool:
    """``open(p)``/``open(p, 'r')`` is a read; a writing mode — or one we cannot see — is a write."""
    mode: Optional[ast.expr] = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if mode is None:
        return False
    literal = _literal_string(mode)
    if literal is None:
        return True  # computed mode: fail closed
    return any(char in _WRITE_MODE_CHARS for char in literal)


def _opens_with_write_flags(node: ast.Call) -> bool:
    if len(node.args) < 2:
        return False
    flag_names = [child.attr for child in ast.walk(node.args[1]) if isinstance(child, ast.Attribute)]
    if flag_names:
        return any(name in _WRITE_FLAG_NAMES for name in flag_names)
    return _literal_string(node.args[1]) is None  # computed flags: fail closed


def _literal_paths(node: Optional[ast.AST]) -> list[str]:
    """String literals inside a path-shaped expression: ``Path('a')/'b'`` yields ''a'', ''b''.

    Matching is basename-based, so the literals of a join are the candidates that matter.
    """
    if node is None:
        return []
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, (ast.BinOp, ast.Tuple, ast.List, ast.Call, ast.Attribute, ast.JoinedStr)):
        return [literal for child in ast.iter_child_nodes(node) for literal in _literal_paths(child)]
    return []


def _literal_string(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_strings(nodes: list[ast.expr]) -> list[str]:
    """String literals in *nodes*, including the elements of an argv list/tuple."""
    out: list[str] = []
    for node in nodes:
        if isinstance(node, (ast.List, ast.Tuple)):
            out.extend(literal for element in node.elts for literal in _literal_strings([element]))
        else:
            literal = _literal_string(node)
            if literal is not None:
                out.append(literal)
    return out


def _shell_payloads(args: list[ast.expr]) -> list[str]:
    """Shell text a launcher call hands to the shell: a command string, or an argv list.

    An argv list is joined with each element quoted, so ``['tee', 'a b.md']`` scans as the
    command ``tee 'a b.md'`` rather than as two words.
    """
    payloads: list[str] = []
    for node in args:
        if isinstance(node, (ast.List, ast.Tuple)):
            elements = _literal_strings([node])
            if elements:
                payloads.append(" ".join(shlex.quote(element) for element in elements))
            continue
        literal = _literal_string(node)
        if literal is not None:
            payloads.append(literal)
    return payloads


# ---- regex fallback (unparseable Python, other languages' inline payloads) ---------

_MODE_OPEN_RE = re.compile(
    r"""(?<![\w.])(?:open|io\.open|codecs\.open|open_sync)\s*\(\s*(?P<path>'[^']*'|"[^"]*")\s*,"""
    r"""\s*(?P<mode>'[^']*'|"[^"]*")""")
_FIRST_ARG_WRITE_RE = re.compile(
    r"""(?<![\w.])(?:write_file|patch|writeFileSync|writeFile|write_all|File\.write|io\.write)"""
    r"""\s*\(\s*(?P<path>'[^']*'|"[^"]*")""")
_RECEIVER_WRITE_RE = re.compile(r"\.write_text\s*\(|\.write_bytes\s*\(|\.open\s*\(|writeFileSync\s*\(")
_STRING_RE = re.compile(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"")


def _regex_code_targets(source: str) -> list[str]:
    targets: list[str] = []
    for match in _MODE_OPEN_RE.finditer(source):
        mode = _unquote(match.group("mode"))
        if any(char in _WRITE_MODE_CHARS for char in mode):
            targets.append(_unquote(match.group("path")))
    targets.extend(_unquote(match.group("path")) for match in _FIRST_ARG_WRITE_RE.finditer(source))
    for match in _RECEIVER_WRITE_RE.finditer(source):
        line_start = source.rfind("\n", 0, match.start()) + 1
        targets.extend(_string_literals(source[line_start:match.start()]))
    return targets


def _string_literals(text: str) -> list[str]:
    return [_unquote(match.group(0)) for match in _STRING_RE.finditer(text)]


def _unquote(token: str) -> str:
    """Strip the surrounding quotes of a Python/shell literal, with the escapes it can carry."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        token = token[1:-1]
    return token.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
