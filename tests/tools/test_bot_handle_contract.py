"""The bot @handle contract, across every local surface that resolves an address.

The default profile's handle is DATA — ``ui_meta['hermes-bots'].handle`` on the install-root
profile.yaml — not the hardcoded ``'hermes'`` constant. It drives all four surfaces:

- the Bot Mode protocol section (signature line + teammate roster bullets);
- the local DM resolver (``message_agent`` → ``_resolve_local_name``);
- the relay roster row the Desktop pushes (``bot_relay._normalize_roster_row``);
- the gateway relay door (``bot_relay.deliver``).

Before the contract each of those hardcoded ``'hermes'`` for 'default', so addressing the
fleet's published handle failed ("No teammate named ..." / "no profile ... on this gateway"),
while the tombstone dir ``profiles/.deleted/`` and leftover dirs with no profile.yaml and no
state.db were treated as delivery targets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tui_gateway.server as srv
from tools import bot_mode_dm, bot_mode_probe

HANDLE = "yoyodine-majordomo"


@pytest.fixture(autouse=True)
def _fresh_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def _bots_yaml(*, shape: str = "cloud", handle: str | None = None) -> str:
    body = f"ui_meta:\n  hermes-bots:\n    shape: {shape}\n"
    return body + f"    handle: {handle!r}\n" if handle is not None else body


def _install(tmp_path, *, root_handle: str | None = None, named=("researcher",), extra_dirs=()) -> Path:
    """An install root whose profile.yaml carries (or omits) the addressable handle."""
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    (home / "profile.yaml").write_text(_bots_yaml(handle=root_handle), encoding="utf-8")
    for name in named:
        d = home / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.yaml").write_text(_bots_yaml(), encoding="utf-8")
    for rel in extra_dirs:
        (home / rel).mkdir(parents=True, exist_ok=True)
    return home


# ── protocol section: signature + roster bullets ─────────────────────────────


def test_signature_line_uses_the_configured_handle(tmp_path):
    home = _install(tmp_path, root_handle=HANDLE)
    section = bot_mode_probe.get_bot_mode_protocol_section(home)
    assert f"You are `@{HANDLE}`." in section
    assert "@hermes" not in section


def test_roster_bullet_uses_the_configured_handle(tmp_path):
    """A teammate must address the root profile by the handle the fleet publishes."""
    home = _install(tmp_path, root_handle=HANDLE)
    section = bot_mode_probe.get_bot_mode_protocol_section(home / "profiles" / "researcher")
    roster_block = section.split("Your teammates")[1]
    assert f"`@{HANDLE}`" in roster_block
    assert "`@hermes`" not in roster_block


def test_unset_handle_keeps_the_legacy_alias(tmp_path):
    home = _install(tmp_path)
    assert "You are `@hermes`." in bot_mode_probe.get_bot_mode_protocol_section(home)


@pytest.mark.parametrize("bad", ["all", "everyone", "user", "default", "not a handle", "-lead", ""])
def test_reserved_or_malformed_handle_is_not_an_address(tmp_path, bad):
    """@all/@everyone address a surface, not a bot; a malformed value is not an address at all."""
    home = _install(tmp_path, root_handle=bad)
    assert "You are `@hermes`." in bot_mode_probe.get_bot_mode_protocol_section(home)


# ── one resolver: name, configured handle, legacy alias ──────────────────────


def test_resolver_accepts_name_handle_and_legacy_alias(tmp_path):
    home = _install(tmp_path, root_handle=HANDLE)
    resolve = bot_mode_probe.resolve_local_profile
    assert resolve(home, HANDLE) == "default"
    assert resolve(home, f"@{HANDLE}") == "default"
    assert resolve(home, HANDLE.upper()) == "default"
    assert resolve(home, "hermes") == "default"  # legacy alias stays a working address
    assert resolve(home, "default") == "default"
    assert resolve(home, "researcher") == "researcher"
    assert resolve(home, "ghost") is None


def test_resolver_skips_tombstone_and_leftover_dirs(tmp_path):
    """``profiles/.deleted/`` is the tombstone `hermes profile delete` leaves behind, and a dir
    with neither profile.yaml nor state.db is a leftover — neither is a delivery target."""
    home = _install(tmp_path, root_handle=HANDLE, extra_dirs=("profiles/.deleted", "profiles/runtime-only"))
    resolve = bot_mode_probe.resolve_local_profile
    assert resolve(home, ".deleted") is None
    assert resolve(home, "runtime-only") is None
    assert [n for n, _d in bot_mode_probe._roster(home)] == ["default", "researcher"]


def test_profile_dir_with_only_a_state_db_is_still_a_target(tmp_path):
    home = _install(tmp_path, named=())
    d = home / "profiles" / "dbonly"
    d.mkdir(parents=True)
    (d / "state.db").write_bytes(b"")
    assert bot_mode_probe.resolve_local_profile(home, "dbonly") == "dbonly"


def test_resolver_fails_closed_on_ambiguity(tmp_path):
    """A handle that collides with another profile's name must not capture its address."""
    home = _install(tmp_path, root_handle="researcher")
    assert bot_mode_probe.resolve_local_profile(home, "researcher") is None


# ── local DM path ────────────────────────────────────────────────────────────


def test_local_dm_resolves_handle_to_the_root_profile(tmp_path):
    home = _install(tmp_path, root_handle=HANDLE)
    roster = [n for n, _d in bot_mode_probe._roster(home)]
    assert bot_mode_dm._resolve_local_name(HANDLE, roster, home) == "default"
    assert bot_mode_dm._resolve_local_name("hermes", roster, home) == "default"
    assert bot_mode_dm._resolve_local_name("researcher", roster, home) == "researcher"
    assert bot_mode_dm._resolve_local_name(f"{HANDLE}-x", roster, home) is None


class _FakeDB:
    def __init__(self, home: Path):
        self.db_path = str(home / "state.db")

    def get_session_title(self, _sid):
        return "Bot Chat"


class _FakeAgent:
    def __init__(self, home: Path):
        self._session_db = _FakeDB(home)
        self.session_id = "sess-1"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools: list = []
        self.valid_tool_names: set = set()


def test_message_agent_delivers_to_default_when_addressed_by_handle(tmp_path, monkeypatch):
    """End to end through the tool: a teammate bot addressing the root profile by the handle the
    fleet publishes lands in the root profile's Bot Chat, signed by the sending bot."""
    home = _install(tmp_path, root_handle=HANDLE, named=("researcher",))
    me_home = home / "profiles" / "researcher"
    seen: dict = {}
    monkeypatch.setattr(bot_mode_dm, "_start_delivery", lambda *a, **kw: seen.update(argv=a[0], label=a[2]) or "{}")
    out = bot_mode_dm.message_agent_tool(
        target=HANDLE, message="status?", agent=_FakeAgent(me_home))
    from tools.bot_relay import BOT_CHAT_TURN_ARGS

    assert "error" not in out, out
    assert seen["argv"][1:3] == ["-p", "default"]
    assert seen["argv"][3:] == list(BOT_CHAT_TURN_ARGS)
    assert seen["label"] == f"@{HANDLE}"


# ── gateway relay door ───────────────────────────────────────────────────────


def test_relay_deliver_accepts_handle_and_legacy_alias(tmp_path, monkeypatch):
    home = _install(tmp_path, root_handle=HANDLE)
    monkeypatch.setenv("HERMES_HOME", str(home))
    calls: dict = {}

    class _Proc:
        returncode = 0
        stdout = "pong"
        stderr = ""

    def _fake_run(argv, **kwargs):
        calls["argv"] = argv
        return _Proc()

    monkeypatch.setattr("subprocess.run", _fake_run)
    out = srv._methods["bot_relay.deliver"](1, {"profile": HANDLE, "message": "ping"})
    assert "error" not in out, out
    assert calls["argv"][1:3] == ["-p", "default"]

    srv._methods["bot_relay.deliver"](2, {"profile": "hermes", "message": "x"})
    assert calls["argv"][1:3] == ["-p", "default"]

    calls.clear()
    err = srv._methods["bot_relay.deliver"](3, {"profile": f"{HANDLE}-x", "message": "x"})
    assert "error" in err and not calls


def test_relay_deliver_refuses_a_tombstone_dir(tmp_path, monkeypatch):
    home = _install(tmp_path, root_handle=HANDLE, extra_dirs=("profiles/.deleted",))
    monkeypatch.setenv("HERMES_HOME", str(home))
    err = srv._methods["bot_relay.deliver"](1, {"profile": ".deleted", "message": "x"})
    assert "error" in err and ".deleted" in err["error"]["message"]


# ── Desktop-pushed relay roster row ──────────────────────────────────────────


def test_relay_roster_row_falls_back_to_the_configured_handle(tmp_path):
    """A Desktop row that omits `handle` must not be stamped @hermes."""
    home = _install(tmp_path, root_handle=HANDLE)
    from tools import bot_relay

    rows = [{"profile": "default", "connection_id": "gw-1"}, {"profile": "researcher", "connection_id": "gw-1"}]
    assert bot_relay.write_remote_roster(home, rows) == 2
    handles = {r["profile"]: r["handle"] for r in bot_relay.read_remote_roster(home)}
    assert handles == {"default": HANDLE, "researcher": "researcher"}
