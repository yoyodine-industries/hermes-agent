"""A retired lane's legacy sessions.json entry must not be re-imported into the routing index.

Regression for the card that followed the persistent WhatsApp source resolution of the retired
lane handle. The routing row was repaired out of ``gateway_routing``, yet the key naming the
retired handle came back and its source re-resolved on every heartbeat poll tick. The only
persisted route back into the index is the legacy ``sessions.json`` mirror, which
``_import_legacy_sessions_json`` folds in for keys the index lacks and the next ``_save``
re-persists into ``state.db``.
"""

import json

import pytest

from gateway.session import SessionEntry
from gateway.session_persistence import SessionPersistenceMixin

RETIRED_KEY = "agent:retired-lane:whatsapp:dm:213976076046365"
LIVE_KEY = "agent:default:whatsapp:dm:213976076046365"


class _RoutingStore(SessionPersistenceMixin):
    """Real import path over a temp sessions dir; no live DB, no live HERMES_HOME."""

    def __init__(self, sessions_dir):
        self.sessions_dir = sessions_dir
        self._entries = {}


def _entry_json(profile):
    return {"origin": {"profile": profile}, "session_id": "s-1", "created_at": 0}


def _install_mirror(tmp_path, monkeypatch, data):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "sessions.json").write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        SessionEntry,
        "from_dict",
        staticmethod(
            lambda d: type("E", (), {"origin": type("O", (), {"profile": d["origin"]["profile"]})()})()
        ),
    )
    return _RoutingStore(sessions_dir)


def test_retired_lane_entry_is_not_reimported(tmp_path, monkeypatch):
    import hermes_cli.profiles as profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "default")
    store = _install_mirror(
        tmp_path, monkeypatch,
        {RETIRED_KEY: _entry_json("retired-lane"), LIVE_KEY: _entry_json("default")},
    )

    store._import_legacy_sessions_json(False)

    assert LIVE_KEY in store._entries, "a live profile's entry must still be imported"
    assert RETIRED_KEY not in store._entries, (
        "an entry naming a non-existent (retired) profile must not be re-imported: "
        "it re-creates the routing key the next _save persists"
    )


def test_entry_with_unresolvable_profile_is_kept(tmp_path, monkeypatch):
    """Never drop an entry whose liveness cannot be established."""
    import hermes_cli.profiles as profiles

    def _boom(name):
        raise OSError("profiles root unavailable")

    monkeypatch.setattr(profiles, "profile_exists", _boom)
    store = _install_mirror(tmp_path, monkeypatch, {RETIRED_KEY: _entry_json("retired-lane")})

    store._import_legacy_sessions_json(False)

    assert RETIRED_KEY in store._entries


def test_driver_caller_label_names_the_driver_not_the_resolver():
    """The fallback diagnostic must name whoever asked for the resolution.

    ``caller_label(2)`` is evaluated inside the argument list of the log call, so its caller is
    always the resolver itself; the label could never identify the loop asking every poll tick.
    """
    from gateway.run_profile_fallback import driver_caller_label

    def _profile_scope_for_source():
        return _resolve_profile_home_for_source()

    def _resolve_profile_home_for_source():
        return driver_caller_label()

    def scan():
        return _profile_scope_for_source()

    label = scan()

    assert "scan" in label, label
    assert "_profile_scope_for_source" not in label, label
    assert "_resolve_profile_home_for_source" not in label, label
