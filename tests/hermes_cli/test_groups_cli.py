"""``hermes groups`` — the headless hosted-room verb (t_ab9ea445).

The verb exists so a Group Chat room can be created and driven on the GATEWAY, without Desktop,
a TUI, a dashboard credential or any WebSocket client. These tests pin the two properties that
make that true, and the lifecycle the operator actually types:

* it operates the room store through :class:`HostedRoomService` WITHOUT starting the worker —
  :meth:`HostedRoomService.start` raising must not affect any command, and the CLI must never
  import the dashboard's session token machinery;
* it reuses the service object's own ``send`` (not a hand-rolled ``append_event``), so the event
  carries the room's authority epoch and stays idempotent on a retry key.
"""

from __future__ import annotations

import argparse
import json

import pytest

from gateway import hosted_rooms

GATEWAY_ID = "install:" + "a" * 32


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A room store at ``tmp_path/shared-state.db`` whose local profiles are ``alpha``/``beta``."""
    for profile in ("alpha", "beta", "gamma"):
        home = tmp_path / "profiles" / profile
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("profile: {}\n", encoding="utf-8")
    monkeypatch.setattr(hosted_rooms, "local_authority_gateway_id", lambda: GATEWAY_ID)
    return tmp_path / "shared-state.db"


def _parser() -> argparse.ArgumentParser:
    from hermes_cli.subcommands.groups import build_groups_parser

    parser = argparse.ArgumentParser()
    build_groups_parser(parser.add_subparsers(dest="command"))
    return parser


def _run(*argv: str) -> int:
    args = _parser().parse_args(["groups", *argv])
    return int(args.func(args))


def _create(store, *extra: str, room_id: str = "room-1") -> int:
    return _run(
        "create", "--db", str(store), "--name", "Platform Team", "--room-id", room_id,
        "--member", "alpha:alpha-bot:Alpha Bot", "--member", "beta:beta-bot", *extra)


def test_create_list_and_state_round_trip(store, capsys):
    assert _create(store, "--json") == 0
    created = json.loads(capsys.readouterr().out)
    members = created["room"]["members"]
    assert [m["member_id"] for m in members] == ["alpha", "beta"]
    assert members[0]["handle"] == "alpha-bot"
    assert members[0]["display_name"] == "Alpha Bot"
    assert members[0]["target"] == {"kind": "local", "profile": "alpha"}
    assert created["room"]["authority_gateway_id"] == GATEWAY_ID

    assert _run("list", "--db", str(store)) == 0
    listed = capsys.readouterr().out
    assert "room-1" in listed and "Platform Team" in listed and "2 members" in listed

    assert _run("state", "--db", str(store), "room-1", "--json") == 0
    state = json.loads(capsys.readouterr().out)["room"]
    assert state["name"] == "Platform Team"
    assert state["authority_epoch"] == 1
    assert "disbanded_at" not in state


def test_create_rejects_a_profile_that_is_not_local(store, capsys):
    rc = _run(
        "create", "--db", str(store), "--name", "nope", "--room-id", "room-bad",
        "--member", "alpha:alpha-bot", "--member", "outsider:outsider-bot")
    assert rc == 2
    assert "outsider" in capsys.readouterr().err


def test_create_requires_two_members(store, capsys):
    rc = _run("create", "--db", str(store), "--name", "solo", "--member", "alpha:alpha-bot")
    assert rc == 2
    assert "between 2 and" in capsys.readouterr().err


def test_member_spec_shape_is_enforced(store, capsys):
    rc = _run(
        "create", "--db", str(store), "--name", "bad", "--member", "alpha", "--member", "beta:b")
    assert rc == 2
    assert "<profile>:<handle>" in capsys.readouterr().err


def test_the_cli_never_starts_the_room_worker(store, monkeypatch):
    """A lease-holding worker in this process would compete with the gateway's own worker."""
    from tui_gateway.hosted_room_service import HostedRoomService

    def _boom(self):  # pragma: no cover - only reached if the CLI starts a worker
        raise AssertionError("the groups CLI must not start a room worker")

    monkeypatch.setattr(HostedRoomService, "start", _boom)
    assert _create(store) == 0


def test_the_cli_carries_no_transport_or_dashboard_credential():
    """The verb is store-only: no JSON-RPC client, no dashboard token, no HTTP transport."""
    import hermes_cli.subcommands.groups as groups_cli

    source = open(groups_cli.__file__, encoding="utf-8").read()
    for forbidden in (
            "HERMES_DASHBOARD_SESSION_TOKEN", "_SESSION_TOKEN", "api/ws", "websockets", "urllib"):
        assert forbidden not in source


def test_send_appends_one_user_event_and_is_idempotent(store, capsys):
    assert _create(store) == 0
    capsys.readouterr()
    assert _run("send", "--db", str(store), "room-1", "hello room",
                "--event-id", "retry-key", "--json") == 0
    first = json.loads(capsys.readouterr().out)["event"]
    assert first["kind"] == "message.user"
    assert first["payload"] == {"text": "hello room", "thread_id": "main"}
    assert first["authority_epoch"] == 1

    assert _run("send", "--db", str(store), "room-1", "hello room", "--event-id", "retry-key") == 0
    capsys.readouterr()
    log = hosted_rooms.read_events(store, room_id="room-1")
    user_events = [e for e in log["events"] if e["kind"] == "message.user"]
    assert len(user_events) == 1


def test_send_on_an_unknown_room_is_refused(store, capsys):
    assert _run("send", "--db", str(store), "room-missing", "hello") == 2
    assert "room-missing" in capsys.readouterr().err


def test_send_refuses_empty_text(store, capsys):
    assert _create(store) == 0
    capsys.readouterr()
    assert _run("send", "--db", str(store), "room-1") == 2
    assert "empty" in capsys.readouterr().err


def test_disband_tombstones_and_is_idempotent(store, capsys):
    assert _create(store) == 0
    capsys.readouterr()
    assert _run("disband", "--db", str(store), "room-1") == 0
    assert "disbanded" in capsys.readouterr().out
    assert _run("list", "--db", str(store)) == 0
    assert "no hosted rooms" in capsys.readouterr().out
    assert _run("disband", "--db", str(store), "room-1") == 0


def test_send_after_disband_is_refused(store, capsys):
    assert _create(store) == 0
    assert _run("disband", "--db", str(store), "room-1") == 0
    capsys.readouterr()
    assert _run("send", "--db", str(store), "room-1", "late") == 2
