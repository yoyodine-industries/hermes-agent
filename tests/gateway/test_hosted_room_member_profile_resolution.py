"""Behavior tests for resolving a member's configured @handle to a local profile name.

A Group Chat member's ``profile`` field is written from the teammate the operator picked. For the
install's own agent that address is its Bot-Mode @handle (``ui_meta['hermes-bots'].handle``, e.g.
``yoyodine-majordomo``) while the profile name is ``default``, so an exact-match roster check rejects
a room the operator just created and the room never dispatches. The validation boundary resolves the
member's address through an injected ``resolve_profile`` callable (the same resolver every other
inbound path uses) and stores the profile NAME.
"""

from __future__ import annotations

import inspect
import time
from pathlib import Path

import pytest

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_driver as driver
from gateway import hosted_rooms


ROOM_ID = "room-1"
GATEWAY_ID = "gateway-a"
HANDLE = "yoyodine-majordomo"
LOCAL_PROFILES = ("default", "ops")
PEER_DIGEST = "a" * 64

RESOLVER_ENTRY_POINTS = (
    "validate_roster",
    "validate_room",
    "plan_next_task",
    "reconstruct_task_plan",
    "plan_publication",
    "derive_member_watermarks",
)


class _Resolver:
    """A stand-in for ``tools.bot_mode_probe.resolve_local_profile``.

    Contract: an address (``@handle``, legacy alias or profile name) -> the profile NAME on this
    gateway; identity for a real profile name; None when it names nothing, or names more than one
    profile. Records every address it is asked about so a test can prove what was (not) resolved.
    """

    def __init__(self, mapping: dict[str, str | None] | None = None) -> None:
        self.mapping: dict[str, str | None] = {profile: profile for profile in LOCAL_PROFILES}
        self.mapping.update(mapping or {})
        self.seen: list[str] = []

    def __call__(self, value: str) -> str | None:
        self.seen.append(str(value))
        return self.mapping.get(str(value).strip().lstrip("@").lower())


def _members(first_profile: str, *, first_handle: str = HANDLE) -> list[dict]:
    return [
        {"member_id": "member-default", "profile": first_profile, "handle": first_handle},
        {"member_id": "member-ops", "profile": "ops", "handle": "ops"},
    ]


def _peer_member() -> dict:
    return {
        "member_id": "member-remote",
        "profile": "remote-agent",
        "handle": "remote-agent",
        "target": {
            "kind": "peer",
            "peer_id": "peer-a",
            "installation_id": "installation-a",
            "profile": "remote-agent",
            "capability_digest": PEER_DIGEST,
        },
    }


def _room_db(tmp_path: Path, members: list[dict]) -> tuple[Path, dict]:
    db = tmp_path / "state.db"
    room = hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Release",
        members=members,
        authority_gateway_id=GATEWAY_ID,
        now=1,
    )
    return db, room


def _events(db: Path) -> list[dict]:
    return hosted_rooms.read_events(
        db, room_id=ROOM_ID, since_seq=0, limit=hosted_rooms.MAX_LOG_LIMIT)["events"]


def _append_user(db: Path, *, event_id: str, text: str, thread_id: str = "thread-1") -> dict:
    return hosted_rooms.append_event(
        db,
        room_id=ROOM_ID,
        event_id=event_id,
        kind="message.user",
        actor={"kind": "user", "id": "local-user"},
        authority_gateway_id=GATEWAY_ID,
        authority_epoch=1,
        payload={"text": text, "thread_id": thread_id},
        now=time.time(),
    )


def test_member_named_by_handle_resolves_to_the_profile_name() -> None:
    resolver = _Resolver({HANDLE: "default"})

    roster = discussion.validate_roster(
        _members(HANDLE), local_profiles=LOCAL_PROFILES, resolve_profile=resolver)

    assert [member.profile for member in roster] == ["default", "ops"]
    assert roster[0].target == {"kind": "local", "profile": "default"}
    assert HANDLE in resolver.seen


def test_unknown_address_fails_closed() -> None:
    resolver = _Resolver()

    with pytest.raises(discussion.DiscussionValidationError) as error:
        discussion.validate_roster(
            _members("ghost-bot"), local_profiles=LOCAL_PROFILES, resolve_profile=resolver)

    assert "ghost-bot" in str(error.value)


def test_ambiguous_address_fails_closed() -> None:
    # The resolver returns None for a handle that also names a real profile (bots/../profile collision).
    resolver = _Resolver({HANDLE: None})

    with pytest.raises(discussion.DiscussionValidationError) as error:
        discussion.validate_roster(
            _members(HANDLE), local_profiles=(*LOCAL_PROFILES, HANDLE), resolve_profile=resolver)

    assert HANDLE in str(error.value)


def test_peer_member_profile_is_never_resolved() -> None:
    resolver = _Resolver({HANDLE: "default"})

    roster = discussion.validate_roster(
        [_peer_member(), *_members("default")],
        local_profiles=LOCAL_PROFILES,
        resolve_profile=resolver,
    )

    assert "remote-agent" not in resolver.seen
    assert roster[0].profile == "remote-agent"
    assert roster[0].target["kind"] == "peer"
    assert roster[0].target["profile"] == "remote-agent"


def test_two_members_resolving_to_one_profile_are_rejected() -> None:
    resolver = _Resolver({HANDLE: "default"})

    with pytest.raises(discussion.DiscussionValidationError, match="member profiles must be unique"):
        discussion.validate_roster(
            [
                {"member_id": "member-handle", "profile": HANDLE, "handle": HANDLE},
                {"member_id": "member-default", "profile": "default", "handle": "default"},
            ],
            local_profiles=LOCAL_PROFILES,
            resolve_profile=resolver,
        )


def test_missing_resolver_keeps_strict_profile_matching() -> None:
    for kwargs in ({}, {"resolve_profile": None}):
        with pytest.raises(discussion.DiscussionValidationError, match="is not local to this gateway"):
            discussion.validate_roster(_members(HANDLE), local_profiles=LOCAL_PROFILES, **kwargs)

    assert discussion.validate_roster(
        _members("default"), local_profiles=LOCAL_PROFILES) == discussion.validate_roster(
            _members("default"), local_profiles=LOCAL_PROFILES, resolve_profile=None)


def test_every_entry_point_takes_resolve_profile_as_a_keyword_only_default_none() -> None:
    for name in RESOLVER_ENTRY_POINTS:
        parameter = inspect.signature(getattr(discussion, name)).parameters.get("resolve_profile")
        assert parameter is not None, f"{name} must accept resolve_profile"
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is None, name


def test_persisted_room_entry_points_resolve_a_handle_member(tmp_path: Path) -> None:
    """The whole persisted-room path: the row keeps the handle address, the plan targets the profile."""
    db, room = _room_db(tmp_path, _members(HANDLE))
    resolver = _Resolver({HANDLE: "default"})
    user = _append_user(db, event_id="user-1", text="Please report.")
    events = _events(db)

    validated = discussion.validate_room(room, local_profiles=LOCAL_PROFILES, resolve_profile=resolver)
    assert validated.members[0].profile == "default"

    watermarks = discussion.derive_member_watermarks(
        room, events, local_profiles=LOCAL_PROFILES, resolve_profile=resolver)
    assert isinstance(watermarks, dict)

    decision = discussion.plan_next_task(
        room, events, local_profiles=LOCAL_PROFILES, resolve_profile=resolver)
    assert decision.status == "task", decision
    assert decision.task is not None
    assert decision.task.member.profile == "default"
    assert decision.task.payload["target_profile"] == "default"
    assert decision.task.payload["target_member_id"] == "member-default"
    assert decision.task.payload["source_event_seq"] == user["seq"]

    stored = driver.admit_task(db, decision.task.identity, payload=decision.task.payload, clock=time.time)
    assert stored["status"] == "queued"
    reconstructed = discussion.reconstruct_task_plan(
        room, events, stored, local_profiles=LOCAL_PROFILES, resolve_profile=resolver)
    assert reconstructed == decision.task

    publication = discussion.plan_publication(
        room,
        events,
        decision.task,
        status="settled",
        result={"text": "Done."},
        local_profiles=LOCAL_PROFILES,
        resolve_profile=resolver,
    )
    assert publication.events
    assert {event.append_kwargs(ROOM_ID)["payload"].get("member_id") for event in publication.events} == {
        "member-default"}


def test_persisted_room_entry_points_reject_a_handle_member_without_the_resolver(tmp_path: Path) -> None:
    db, room = _room_db(tmp_path, _members(HANDLE))
    _append_user(db, event_id="user-1", text="Please report.")
    events = _events(db)

    for call in (
        lambda: discussion.validate_room(room, local_profiles=LOCAL_PROFILES),
        lambda: discussion.plan_next_task(room, events, local_profiles=LOCAL_PROFILES),
        lambda: discussion.derive_member_watermarks(room, events, local_profiles=LOCAL_PROFILES),
    ):
        with pytest.raises(discussion.DiscussionValidationError, match="is not local to this gateway"):
            call()
