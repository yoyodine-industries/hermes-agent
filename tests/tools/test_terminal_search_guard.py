"""Contract tests for the unbounded recursive-search guard.

A recursive walk rooted at a whole home directory, at the Hermes home tree, or at ``/``
reads everything below it. One such ``grep -r`` — root ``.``, cwd ``~/.hermes`` — held a
full core for 12m32s on this host and returned what a depth-bounded search returns in
seconds, so ``tools/terminal_search_guard`` refuses that walk at the terminal-tool
boundary. It refuses only that walk: a named subtree (a project, a workspace, or a
directory *inside* the Hermes home) is never broad, a bounded walk of a broad root
passes, and text inside a quoted argument is not a command.

False-positive contract, which the allowed cases pin down: a legitimate ``grep -r`` in a
small project directory runs untouched — the guard keys on the root of the walk and on
the absence of a bound, never on the tool name or on the recursion flag alone.

Every case is asserted twice: at the detector, and through
``terminal_tool._pre_exec_block`` — the guard site the terminal tool itself calls — so a
guard that stops being wired into the tool path fails here instead of passing silently.
"""

import json
from pathlib import Path

import pytest

from hermes_constants import get_hermes_home
from tools import terminal_search_guard as search_guard
from tools.terminal_tool import _Rejected, _pre_exec_block

SESSION_KEY = "unbounded-search-guard-test"

# The two commands a card worker actually issued, verbatim. Both take their root from a
# cwd of ``~/.hermes``, which is why the guard resolves ``.`` against the real cwd.
REAL_SWEEP = (
    'grep -rl "maintenance-nightly" . 2>/dev/null'
    ' | grep -v "/sessions/" | grep -v "/logs/" | head -20'
)
REAL_RECORD_LOOKUP = 'grep -rl "APR-0240" . | head -10'


def _hermes_home() -> str:
    return str(get_hermes_home())


def _cwd(kind: str) -> str:
    """A working directory of the named shape: the Hermes home, a lane profile inside
    it (so ``../..`` lands on the home), or an unrelated project directory."""
    home = get_hermes_home()
    if kind == "hermes_home":
        return str(home)
    if kind == "hermes_profile":
        return str(home / "profiles" / "platform-coder")
    assert kind == "project", kind
    return str(home.parent / "project-worktree")


def _fill(command: str) -> str:
    return (
        command.replace("<HERMES_HOME>", _hermes_home())
        .replace("<LANE_HOME>", str(get_hermes_home() / "profiles" / "platform-coder"))
        .replace("<HOME>", str(Path.home()))
    )


def _terminal_refusal(command: str, cwd: str) -> str:
    """Run *command* through the terminal tool's own guard; return the refusal message."""
    with pytest.raises(_Rejected) as rejected:
        _pre_exec_block(
            command, env=None, env_type="local", cwd=cwd, workdir=None, session_key=SESSION_KEY,
        )
    payload = json.loads(rejected.value.result_json)
    assert payload["exit_code"] == 1
    assert payload["status"] == "blocked"
    return payload["error"]


REFUSED = (
    pytest.param(REAL_SWEEP, "hermes_home", "the Hermes home", id="real-maintenance-sweep"),
    pytest.param(REAL_RECORD_LOOKUP, "hermes_home", "the Hermes home", id="real-record-lookup"),
    pytest.param("find / -name 'hermes_state.py' 2>/dev/null", "project", "filesystem root", id="find-root"),
    pytest.param("find -L / -name 'hermes_state.py'", "project", "filesystem root", id="find-root-global-option"),
    pytest.param("grep -rn 'APR-0240' ~", "project", "whole home directory", id="grep-tilde"),
    pytest.param("grep -rn 'APR-0240' $HOME", "project", "whole home directory", id="grep-dollar-home"),
    pytest.param("grep -rn 'APR-0240' /Users", "project", "whole home and repo trees", id="grep-users"),
    pytest.param("rg -n 'APR-0240' /", "project", "filesystem root", id="rg-root"),
    pytest.param("rg 'APR-0240' ~", "project", "whole home directory", id="rg-no-file-list"),
    pytest.param("find . -name '*.json'", "hermes_home", "the Hermes home", id="find-dot-in-home"),
    pytest.param("grep -rn 'APR-0240' ../..", "hermes_profile", "the Hermes home", id="parent-dir-root"),
    pytest.param("grep -rn 'APR-0240' <HERMES_HOME>", "project", "the Hermes home", id="hermes-home-root"),
    pytest.param("bash -c \"find / -name 'hermes_state.py'\"", "project", "filesystem root", id="shell-payload"),
    pytest.param("grep -rn 'APR-0240' .", "hermes_profile", "lane profile home", id="bare-dot-in-profile"),
    pytest.param("grep -rn 'APR-0240' <LANE_HOME>", "project", "lane profile home", id="lane-home-root"),
)


@pytest.mark.parametrize("command, cwd_kind, expected_label", REFUSED)
def test_refuses_recursive_walk_of_a_broad_root(command, cwd_kind, expected_label):
    command = _fill(command)
    cwd = _cwd(cwd_kind)

    hit = search_guard.scan_unbounded_search(command, cwd)
    assert hit is not None, command
    assert expected_label in hit[1], hit

    message = _terminal_refusal(command, cwd)
    assert "Blocked" in message
    assert "-maxdepth" in message, "the refusal must name the bounded form"


ALLOWED = (
    pytest.param(
        "grep -rl -m1 --include='*.md' APR-0240 <HERMES_HOME>/kanban 2>/dev/null",
        "project", id="narrow-subtree-inside-hermes-home",
    ),
    pytest.param("grep -rn 'APR-0240' <HERMES_HOME>/skills", "project", id="hermes-home-subtree"),
    pytest.param("grep -rn 'APR-0240' <LANE_HOME>/skills", "project", id="lane-home-subtree"),
    pytest.param("find <HOME> -maxdepth 3 -name '*.plist'", "project", id="bounded-home-walk"),
    pytest.param("find . -maxdepth 2 -name '*.py'", "hermes_home", id="bounded-dot-walk"),
    pytest.param("rg --max-depth 2 'APR-0240' /", "project", id="bounded-rg-root"),
    pytest.param("grep -r 'APR-0240' .", "project", id="narrow-recursive-grep"),
    pytest.param("rg -n 'def main' tools/", "project", id="rg-in-project"),
    pytest.param("find <HERMES_HOME>/logs -name '*.log'", "project", id="unbounded-find-of-a-subtree"),
    pytest.param("grep -rn 'APR-0240' <HOME>/notes/report.md", "project", id="single-file-root"),
    pytest.param("grep -n 'APR-0240' notes.md", "hermes_home", id="not-recursive"),
    pytest.param("echo \"grep -rl APR-0240 /\"", "hermes_home", id="quoted-text-not-a-command"),
)


@pytest.mark.parametrize("command, cwd_kind", ALLOWED)
def test_admits_bounded_or_narrowly_rooted_searches(command, cwd_kind):
    command = _fill(command)
    cwd = _cwd(cwd_kind)

    assert search_guard.scan_unbounded_search(command, cwd) is None
    _pre_exec_block(  # must not raise: the tool path lets it run
        command, env=None, env_type="local", cwd=cwd, workdir=None, session_key=SESSION_KEY,
    )
