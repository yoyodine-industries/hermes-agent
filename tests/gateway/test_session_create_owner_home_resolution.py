"""A profile-less ``session.create`` carrying a known session id must resolve to the BOT's home.

Mirror of ``test_session_owner_home_resolution.py`` for the CREATE path. The same fork that
``session.resume`` closes also happens on ``session.create``: the caller's list lookup misses the
hidden canonical "Bot Chat", so it re-materializes the chat by CREATE — with the id it already
holds — and the launch (default) home forks the row. ``_create_owner_home`` resolves the owning
profile home (explicit profile wins; otherwise only when the id lives in exactly one sibling store).
"""

from tui_gateway.methods_session import _create_owner_home


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


def test_create_explicit_profile_wins(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, tmp_path, {"bot": {"canonical"}})
    # An explicit profile already names the home; never overridden by a requested id.
    assert _create_owner_home("/explicit/home", "canonical", _FakeDB(set())) == "/explicit/home"


def test_create_no_profile_no_id_is_none():
    assert _create_owner_home(None, None, _FakeDB(set())) is None


def test_create_launch_home_wins_when_id_present(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, tmp_path, {"bot": {"canonical"}})
    # The id already lives in the launch (default) home: no rebind.
    launch_db = _FakeDB({"canonical"})
    assert _create_owner_home(None, "canonical", launch_db) is None


def test_create_resolves_owning_profile_home(monkeypatch, tmp_path):
    homes, _ = _patch_profiles(
        monkeypatch, tmp_path, {"bot-profile": {"api_0000000000_deadbeef"}, "other": set()}
    )
    # Not in the launch home; owned by exactly one sibling profile -> bind there.
    launch_db = _FakeDB(set())
    assert _create_owner_home(None, "api_0000000000_deadbeef", launch_db) == str(homes["bot-profile"])


def test_create_fails_closed_when_ambiguous(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, tmp_path, {"bot_a": {"dup"}, "bot_b": {"dup"}})
    # Owned by two siblings -> refuse to guess (never rebind a genuinely ambiguous id).
    launch_db = _FakeDB(set())
    assert _create_owner_home(None, "dup", launch_db) is None


def test_create_none_when_absent_everywhere(monkeypatch, tmp_path):
    _patch_profiles(monkeypatch, tmp_path, {"bot": {"other"}})
    # A genuinely new id (absent everywhere) is never silently rebound.
    launch_db = _FakeDB(set())
    assert _create_owner_home(None, "ghost-id", launch_db) is None
