"""A bot's canonical session id must resolve to the BOT's profile home, not the launch home.

Root cause of the bot-chat fork: a multiplexed launch profile (desktop/dashboard, running
under the DEFAULT home) opening a bot's canonical "Bot Chat" by id WITHOUT a ``profile``
bound the session to its own home — forking the chat and registering the live-delivery
consumer lease in the wrong store. ``_find_session_owner_home`` scans sibling profile stores
and returns the owning home (None = already in launch / absent / ambiguous — never guess).
"""

from tui_gateway.methods_session import _find_session_owner_home


class _FakeDB:
    """SessionDB stand-in: ``get_session`` returns a truthy row iff the id is present."""

    def __init__(self, present_ids):
        self._present = set(present_ids)
        self.closed = False

    def get_session(self, sid):
        return {"id": sid} if sid in self._present else None

    def close(self):
        self.closed = True


def _patch_profiles(monkeypatch, tmp_path, owned):
    """Stub profile enumeration + home resolution + db acquire.

    ``owned`` maps a profile name -> set of session ids owned by that profile's store.
    The launch home is a distinct (empty) path, so every listed profile is a "sibling".
    """
    launch = tmp_path / "launch"
    homes = {name: tmp_path / f"home-{name}" for name in owned}
    for home in homes.values():
        home.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: str(launch))
    monkeypatch.setattr("hermes_cli.profiles.list_profile_names", lambda: list(owned))
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: str(homes[name]))

    dbs = {name: _FakeDB(ids) for name, ids in owned.items()}

    def fake_acquire(db_path):
        for name, home in homes.items():
            if str(db_path) == str(home / "state.db"):
                return dbs[name]
        raise RuntimeError(f"unexpected db_path: {db_path}")

    monkeypatch.setattr("hermes_state_registry.acquire", fake_acquire)
    return homes, dbs


def test_launch_home_wins_when_id_present(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, tmp_path, {"bot": {"canonical"}})
    # The id already lives in the launch (default) home: no rebind.
    launch_db = _FakeDB({"canonical"})
    assert _find_session_owner_home("canonical", launch_db) is None


def test_resolves_owning_profile_home(monkeypatch, tmp_path):
    homes, _ = _patch_profiles(
        monkeypatch, tmp_path, {"bot-profile": {"api_0000000000_deadbeef"}, "other": set()}
    )
    # Not in the launch home; owned by exactly one sibling profile -> bind there.
    launch_db = _FakeDB(set())
    assert _find_session_owner_home("api_0000000000_deadbeef", launch_db) == str(homes["bot-profile"])


def test_fails_closed_when_ambiguous(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, tmp_path, {"bot_a": {"dup"}, "bot_b": {"dup"}})
    # Owned by two siblings -> refuse to guess.
    launch_db = _FakeDB(set())
    assert _find_session_owner_home("dup", launch_db) is None


def test_none_when_absent_everywhere(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, tmp_path, {"bot": {"other"}})
    launch_db = _FakeDB(set())
    assert _find_session_owner_home("ghost-id", launch_db) is None
