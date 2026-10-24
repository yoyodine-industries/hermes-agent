"""The consent-gated SURFACE class: what a write touches, and what token covers it.

Contract under test (card ``t_c64d7c79``, design of record ``t_cb9259fc``):

* the class is CLOSED at six surfaces (D1) and each surface maps to its D2 scope
  token -- root ``config.yaml``/``.env`` carry the bare token, per-profile
  spellings carry ``:<profile>``, a plugin write names the plugin, a plist names
  the file, and ``hermes config set|unset|edit`` carries the dotted key;
* classification is a pure decision: no filesystem, environment or database read,
  so one definition serves the FILE, TERMINAL and EXEC doors and can be tested
  without a hermes home;
* reads, look-alike paths outside the hermes home, and a surface used as a copy
  SOURCE are not in the class -- the measured false-positive class that would make
  the clause unusable.
"""

from __future__ import annotations

import contextlib
from dataclasses import FrozenInstanceError
from unittest import mock

import pytest

from hermes_cli import consent_surface as cs

SIX_SURFACES = {
    "hermes-config",
    "hermes-env",
    "hermes-soul",
    "hermes-plugin",
    "launchd-plist",
    "hermes-cli-config",
}

CONFIG = "hermes-config"
ENV = "hermes-env"
SOUL = "hermes-soul"
PLUGIN = "hermes-plugin"
PLIST = "launchd-plist"
CLI_CONFIG = "hermes-cli-config"


# --- D1: the closed set -----------------------------------------------------


def test_the_class_is_closed_at_six_surfaces():
    assert cs.SURFACES == frozenset(SIX_SURFACES)
    assert isinstance(cs.SURFACES, frozenset)


def test_a_surface_outside_the_class_is_refused():
    with pytest.raises(ValueError):
        cs.SurfaceHit(surface="hermes-skills", scope_token="x", path=None, reason="r")


def test_a_hit_is_frozen():
    hit = cs.classify_path("~/.hermes/config.yaml")
    assert hit is not None
    with pytest.raises(FrozenInstanceError):
        hit.surface = ENV  # type: ignore[misc]


# --- D2: paths --------------------------------------------------------------

PATH_HITS = [
    # (path, surface, scope token)
    # root-home files carry :root -- a bare token would cover every lane's file
    ("~/.hermes/config.yaml", CONFIG, "hermes-config:root"),
    ("$HERMES_HOME/config.yaml", CONFIG, "hermes-config:root"),
    ("${HERMES_HOME}/config.yaml", CONFIG, "hermes-config:root"),
    ("/Users/hermes_user/.hermes/config.yaml", CONFIG, "hermes-config:root"),
    ("~/.hermes/profiles/platform-coder/config.yaml", CONFIG, "hermes-config:platform-coder"),
    ("~/.hermes/.env", ENV, "hermes-env:root"),
    ("/Users/hermes_user/.hermes/.env", ENV, "hermes-env:root"),
    ("~/.hermes/profiles/platform-coder/.env", ENV, "hermes-env:platform-coder"),
    # the root home is a live seat's home; its SOUL.md is a persona write
    ("~/.hermes/SOUL.md", SOUL, "hermes-soul:root"),
    ("$HERMES_HOME/SOUL.md", SOUL, "hermes-soul:root"),
    ("~/.hermes/profiles/platform-coder/SOUL.md", SOUL, "hermes-soul:platform-coder"),
    ("/Users/hermes_user/.hermes/profiles/default/SOUL.md", SOUL, "hermes-soul:default"),
    ("~/.hermes/plugins/evil/handler.py", PLUGIN, "hermes-plugin:evil"),
    ("~/.hermes/hermes-agent/plugins/evil.py", PLUGIN, "hermes-plugin:evil"),
    ("/Users/hermes_user/.hermes/hermes-agent/plugins/evil/handler.py", PLUGIN, "hermes-plugin:evil"),
    ("~/Library/LaunchAgents/ai.hermes.gateway.plist", PLIST, "launchd-plist:ai.hermes.gateway.plist"),
    ("/Users/hermes_user/Library/LaunchAgents/ai.hermes.gateway.plist", PLIST, "launchd-plist:ai.hermes.gateway.plist"),
]


@pytest.mark.parametrize(("path", "surface", "token"), PATH_HITS)
def test_a_surface_path_is_classified_with_its_scope_token(path, surface, token):
    hit = cs.classify_path(path)
    assert hit is not None, f"{path} must be in the consent-gated class"
    assert hit.surface == surface
    assert hit.scope_token == token
    assert hit.path == path
    assert hit.reason


PATH_MISSES = [
    # profile content that is not one of the three named files
    "~/.hermes/profiles/platform-coder/skills/foo/SKILL.md",
    "~/.hermes/profiles/platform-coder/AGENTS.md",
    "~/.hermes/profiles/platform-coder/state.db",
    "~/.hermes/profiles/default/memories/notes.md",
    # the plugins DIRECTORY is not a write target
    "~/.hermes/plugins",
    "~/.hermes/plugins/",
    # other hermes home content
    "~/.hermes/memories/notes.md",
    "~/.hermes/logs/gateway.log",
    "~/.hermes/kanban/boards/platform/kanban.db",
    # look-alikes outside the hermes home
    "~/projects/myapp/config.yaml",
    "~/projects/myapp/.env",
    "~/projects/myapp/SOUL.md",
    "~/projects/myapp/plugins/x.py",
    "/etc/config.yaml",
    "/tmp/.env",
    # a LaunchAgents file that is not a plist
    "~/Library/LaunchAgents/notes.txt",
    # the repo's own source tree is not a surface
    "<repo>/tools/approval.py",
]


@pytest.mark.parametrize("path", PATH_MISSES)
def test_a_path_outside_the_class_is_not_a_hit(path):
    assert cs.classify_path(path) is None, f"{path} must NOT be consent-gated"


# --- the lexical default must not swallow fixture trees ---------------------
#
# ``.hermes``/``hermes-agent`` are the standard names for a hermes tree, so a repo,
# a test fixture or a checkout may carry either segment.  With no root supplied the
# marker scan is the only answer available and has to fire; once a caller supplies
# the real root, that root is authoritative and the marker scan must stand down, or
# the FILE door refuses every fixture that happens to be laid out like a hermes home.

FIXTURE_TREES = [
    ("/tmp/checkout/.hermes/config.yaml", "hermes_home"),
    ("/tmp/checkout/.hermes/profiles/p/SOUL.md", "hermes_home"),
    ("/tmp/checkout/.hermes/plugins/evil/handler.py", "hermes_home"),
    ("/tmp/checkout/hermes-agent/plugins/evil.py", "install_root"),
]


@pytest.mark.parametrize(("path", "root_kind"), FIXTURE_TREES)
def test_an_explicit_root_disowns_a_look_alike_tree(path, root_kind):
    roots = {
        "hermes_home": {"hermes_home": "/Users/hermes_user/.hermes"},
        "install_root": {"install_root": "/Users/hermes_user/.hermes/hermes-agent"},
    }[root_kind]
    assert cs.classify_path(path, **roots) is None, f"{path} is not under the live {root_kind}"
    # ...and without the root the marker scan still finds it (the documented default)
    assert cs.classify_path(path) is not None


def test_an_explicit_root_still_matches_its_own_spellings():
    """Authoritative does not mean literal-only: ``~``/``$HERMES_HOME`` still work."""
    home = "/Users/hermes_user/.hermes"
    for spelling in (
        "/Users/hermes_user/.hermes/config.yaml",
        "~/.hermes/config.yaml",
        "$HERMES_HOME/config.yaml",
        "${HERMES_HOME}/config.yaml",
    ):
        hit = cs.classify_path(spelling, hermes_home=home)
        assert hit is not None, spelling
        assert hit.scope_token == "hermes-config:root", spelling


def test_injected_roots_cover_a_non_standard_hermes_home():
    # without the injected root there is no structural marker to match
    assert cs.classify_path("/opt/heroes/config.yaml") is None
    hit = cs.classify_path("/opt/heroes/config.yaml", hermes_home="/opt/heroes")
    assert hit is not None and hit.surface == CONFIG and hit.scope_token == "hermes-config:root"
    hit = cs.classify_path("/opt/heroes/profiles/p/SOUL.md", hermes_home="/opt/heroes")
    assert hit is not None and hit.scope_token == "hermes-soul:p"


def test_a_root_write_never_names_the_bare_coverage_token():
    """The refusal tells the operator what to file: a root write names ``:root``.

    The bare token IS the domain name and covers every profile, so naming it for a
    root write would license the whole fleet off one approval.
    """
    for path in ("~/.hermes/config.yaml", "~/.hermes/.env", "~/.hermes/SOUL.md"):
        hit = cs.classify_path(path)
        assert hit is not None and hit.scope_token == f"{hit.surface}:root", path
        assert hit.scope_token != hit.surface, path


def test_the_command_sweep_honours_an_explicit_root():
    """A fixture tree is not the live home even when a command writes into it."""
    command = "echo x > /tmp/checkout/.hermes/config.yaml"
    assert cs.classify_command(command, command, hermes_home="/Users/hermes_user/.hermes") is None
    assert cs.classify_command(command, command) is not None


def test_injected_install_root_covers_its_plugin_tree():
    hit = cs.classify_path("/opt/build/checkout/plugins/x/y.py", install_root="/opt/build/checkout")
    assert hit is not None and hit.scope_token == "hermes-plugin:x"


# --- D2: commands -----------------------------------------------------------

COMMAND_HITS = [
    # (argv, verbatim text, surface, scope token)
    (["hermes", "config", "set", "approvals.mode", "off"], "hermes config set approvals.mode off", CLI_CONFIG, "hermes-config:approvals.mode"),
    (["hermes", "config", "unset", "kanban.max_spawn"], "hermes config unset kanban.max_spawn", CLI_CONFIG, "hermes-config:kanban.max_spawn"),
    (["hermes", "config", "set", "mcp.servers.x.command", "evil"], "hermes config set mcp.servers.x.command evil", CLI_CONFIG, "hermes-config:mcp.servers.x.command"),
    (["hermes", "config", "edit"], "hermes config edit", CLI_CONFIG, "hermes-config"),
    # ``-p``/``--profile`` take their value as a separate token; the value must not
    # be read as the command word (``-p p config set ...``) nor as the dotted key
    # (``config set -p p a.b c``).  ``hermes -p`` is the host's normal profile flag.
    (["hermes", "-p", "platform-coder", "config", "set", "a.b", "c"], "hermes -p platform-coder config set a.b c", CLI_CONFIG, "hermes-config:a.b"),
    (["hermes", "--profile", "platform-coder", "config", "edit"], "hermes --profile platform-coder config edit", CLI_CONFIG, "hermes-config"),
    (["hermes", "config", "set", "-p", "platform-coder", "a.b", "c"], "hermes config set -p platform-coder a.b c", CLI_CONFIG, "hermes-config:a.b"),
    (["hermes", "-p", "platform-coder", "config", "unset", "kanban.max_spawn"], "hermes -p platform-coder config unset kanban.max_spawn", CLI_CONFIG, "hermes-config:kanban.max_spawn"),
    (["/Users/hermes_user/.hermes/bin/hermes", "config", "set", "a.b", "c"], "/Users/hermes_user/.hermes/bin/hermes config set a.b c", CLI_CONFIG, "hermes-config:a.b"),
    (["cp", "evil.plist", "~/Library/LaunchAgents/ai.hermes.gateway.plist"], "cp evil.plist ~/Library/LaunchAgents/ai.hermes.gateway.plist", PLIST, "launchd-plist:ai.hermes.gateway.plist"),
    (["mv", "/tmp/x.yaml", "~/.hermes/config.yaml"], "mv /tmp/x.yaml ~/.hermes/config.yaml", CONFIG, "hermes-config:root"),
    (["install", "-m", "644", "evil.py", "~/.hermes/plugins/evil/handler.py"], "install -m 644 evil.py ~/.hermes/plugins/evil/handler.py", PLUGIN, "hermes-plugin:evil"),
    (["echo", "x"], "echo 'approvals:' >> ~/.hermes/config.yaml", CONFIG, "hermes-config:root"),
    (["echo", "x"], "echo x > ~/.hermes/profiles/p/SOUL.md", SOUL, "hermes-soul:p"),
    (["echo", "x"], "echo x > ~/.hermes/SOUL.md", SOUL, "hermes-soul:root"),
    (["cat", "evil.py"], "cat evil.py > ~/.hermes/plugins/evil/handler.py", PLUGIN, "hermes-plugin:evil"),
    (["echo", "X=1"], "echo X=1 > ~/.hermes/profiles/p/.env", ENV, "hermes-env:p"),
    (["echo", "x"], "echo x | tee ~/Library/LaunchAgents/ai.hermes.gateway.plist", PLIST, "launchd-plist:ai.hermes.gateway.plist"),
    (["sed", "-i", "", "s/a/b/", "~/.hermes/config.yaml"], "sed -i '' 's/a/b/' ~/.hermes/config.yaml", CONFIG, "hermes-config:root"),
    (["sed", "-i", "", "s/a/b/", "~/.hermes/.env"], "sed -i '' 's/a/b/' ~/.hermes/.env", ENV, "hermes-env:root"),
    (["sed", "-i", "", "s/a/b/", "~/.hermes/SOUL.md"], "sed -i '' 's/a/b/' ~/.hermes/SOUL.md", SOUL, "hermes-soul:root"),
    (["printf", "x"], "printf 'x' > /Users/hermes_user/.hermes/hermes-agent/plugins/evil.py", PLUGIN, "hermes-plugin:evil"),
]


@pytest.mark.parametrize(("argv", "text", "surface", "token"), COMMAND_HITS)
def test_a_command_that_writes_a_surface_is_classified(argv, text, surface, token):
    hit = cs.classify_command(argv, text)
    assert hit is not None, f"{text!r} must be in the consent-gated class"
    assert hit.surface == surface
    assert hit.scope_token == token


COMMAND_MISSES = [
    # reads
    (["cat", "~/.hermes/.env"], "cat ~/.hermes/.env"),
    (["grep", "config.yaml", "README"], "grep config.yaml README"),
    (["grep", "-i", "plugins", "~/.hermes/config.yaml"], "grep -i plugins ~/.hermes/config.yaml"),
    (["hermes", "config", "get", "approvals.mode"], "hermes config get approvals.mode"),
    (["hermes", "config", "show"], "hermes config show"),
    (["hermes", "config", "set", "--help"], "hermes config set --help"),
    # the profile flag is a READ here -- skipping its value must not invent a hit
    (["hermes", "-p", "platform-coder", "config", "get", "approvals.mode"], "hermes -p platform-coder config get approvals.mode"),
    # a surface read redirected elsewhere is not a write to the surface
    (["cat", "~/.hermes/.env"], "cat ~/.hermes/.env > /tmp/env.backup"),
    # a surface used as a copy SOURCE is not a write to the surface
    (["cp", "~/.hermes/config.yaml", "/tmp/backup.yaml"], "cp ~/.hermes/config.yaml /tmp/backup.yaml"),
    (["mv", "~/.hermes/plugins/evil", "/tmp/evil"], "mv ~/.hermes/plugins/evil /tmp/evil"),
    # writes that are not to a surface
    (["echo", "hello"], "echo hello > /tmp/notes.txt"),
    (["cp", "a.py", "/tmp/b.py"], "cp a.py /tmp/b.py"),
    (["npm", "test"], "npm test"),
    (["ls", "~/.hermes/profiles"], "ls ~/.hermes/profiles"),
    # ``hermes`` followed by a different command word
    (["hermes", "kanban", "config", "set", "x", "y"], "hermes kanban config set x y"),
]


@pytest.mark.parametrize(("argv", "text"), COMMAND_MISSES)
def test_a_read_or_an_unrelated_command_is_not_a_hit(argv, text):
    assert cs.classify_command(argv, text) is None, f"{text!r} must NOT be gated"


def test_command_text_alone_is_enough():
    hit = cs.classify_command("echo x > ~/.hermes/profiles/p/SOUL.md")
    assert hit is not None and hit.scope_token == "hermes-soul:p"


# The design's own measurement (evidence/surface_probe.out, 2026-10-01): the paths and
# commands the live guards leave open.  Replayed verbatim so the gap this card closes
# cannot silently reopen.
MEASURED_UNGUARDED = [
    ("/Users/hermes_user/.hermes/config.yaml", CONFIG),
    ("/Users/hermes_user/.hermes/profiles/platform-stl/config.yaml", CONFIG),
    ("/Users/hermes_user/.hermes/.env", ENV),
    ("/Users/hermes_user/.hermes/profiles/platform-stl/.env", ENV),
    ("/Users/hermes_user/.hermes/profiles/platform-stl/SOUL.md", SOUL),
    ("/Users/hermes_user/Library/LaunchAgents/ai.hermes.gateway.plist", PLIST),
    ("/Users/hermes_user/.hermes/plugins/evil/handler.py", PLUGIN),
    ("/Users/hermes_user/.hermes/hermes-agent/plugins/evil/handler.py", PLUGIN),
]


@pytest.mark.parametrize(("path", "surface"), MEASURED_UNGUARDED)
def test_every_measured_open_path_is_now_classified(path, surface):
    hit = cs.classify_path(path)
    assert hit is not None and hit.surface == surface


MEASURED_UNGUARDED_COMMANDS = [
    ("hermes config set approvals.mode off", CLI_CONFIG),
    ("echo 'approvals:' >> ~/.hermes/config.yaml", CONFIG),
    ("sed -i '' 's/a/b/' ~/.hermes/config.yaml", CONFIG),
    ("cp evil.plist ~/Library/LaunchAgents/ai.hermes.gateway.plist", PLIST),
    ("echo x > ~/.hermes/profiles/platform-stl/SOUL.md", SOUL),
    ("cat evil.py > ~/.hermes/plugins/evil/handler.py", PLUGIN),
    ("printf 'x' > ~/.hermes/hermes-agent/plugins/evil.py", PLUGIN),
]


@pytest.mark.parametrize(("command", "surface"), MEASURED_UNGUARDED_COMMANDS)
def test_every_measured_unguarded_write_shape_is_now_classified(command, surface):
    hit = cs.classify_command(command, command)
    assert hit is not None, f"{command!r} was measured open and must be gated"
    assert hit.surface == surface


def test_a_supervised_restart_is_left_to_the_hardline_key():
    """``launchctl bootstrap`` is already refused by its own hardline pattern."""
    command = "launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.hermes.gateway.plist"
    assert cs.classify_command(command, command) is None


def test_unbalanced_quotes_do_not_raise():
    assert cs.classify_command("echo 'unterminated", "echo 'unterminated") is None


# --- purity -----------------------------------------------------------------

_IO_DOORS = (
    "builtins.open",
    "os.listdir",
    "os.scandir",
    "sqlite3.connect",
)


def test_classification_opens_no_file_and_no_database():
    """Purity is a behaviour contract, not a source shape: shut every I/O door."""
    expected = cs.classify_path("~/.hermes/profiles/p/config.yaml")
    assert expected is not None
    # warm the lazily-reused detector so this measures classification, not module import
    cs.classify_command(["sed", "-i", "", "s/a/b/", "~/.hermes/config.yaml"], "sed -i '' 's/a/b/' ~/.hermes/config.yaml")
    with contextlib.ExitStack() as stack:
        for door in _IO_DOORS:
            stack.enter_context(
                mock.patch(door, side_effect=AssertionError(f"classification touched {door}"))
            )
        assert cs.classify_path("~/.hermes/profiles/p/config.yaml") == expected
        assert cs.classify_path("~/projects/myapp/config.yaml") is None
        assert cs.classify_command(["hermes", "config", "set", "a.b", "c"], "hermes config set a.b c") is not None


def test_classification_does_not_depend_on_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home" / ".hermes"))
    before = cs.classify_path("~/.hermes/profiles/p/SOUL.md")
    command_before = cs.classify_command(["echo", "x"], "echo x > ~/.hermes/profiles/p/SOUL.md")
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other" / ".elsewhere"))
    assert cs.classify_path("~/.hermes/profiles/p/SOUL.md") == before
    assert cs.classify_command(["echo", "x"], "echo x > ~/.hermes/profiles/p/SOUL.md") == command_before
    assert before is not None and before.scope_token == "hermes-soul:p"
