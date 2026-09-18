"""A boundary that hands an env to a DIFFERENT profile's session must not hand over the sender's
identity (kanban t_f6011a57).

``HERMES_HOME``/``HERMES_PROFILE``/``HERMES_PROFILE_NAME`` describe the process that built the env.
The delivery transports name their target with ``-p``, so an inherited pair can only misdescribe the
child — and a child whose resolution falls through runs as, and attributes its rows to, the sender.
"""

from __future__ import annotations

import os

from tools import bot_mode_dm, bot_relay

IDENTITY = ("HERMES_HOME", "HERMES_PROFILE", "HERMES_PROFILE_NAME")
SENDER_HOME = "/senders/home/profiles/peer-lane"


def _seed_sender_identity(monkeypatch):
    for name in IDENTITY:
        monkeypatch.setenv(name, SENDER_HOME if name.endswith("HOME") else "peer-lane")
    monkeypatch.setenv("PATH", "/usr/bin")


def test_delivery_turn_env_drops_the_senders_identity(monkeypatch):
    _seed_sender_identity(monkeypatch)
    env = bot_mode_dm._delivery_turn_env()
    for name in IDENTITY:
        assert name not in env
    # Everything else still rides, and the turn is still marked as a delivery.
    assert env["PATH"] == "/usr/bin"
    assert env["HERMES_DELIVERY_TURN"] == "1"


def test_delivery_turn_env_scrubs_the_callers_base(monkeypatch):
    """The relay hands in its own author-carrying env; identity must not survive that either."""
    base = {"PATH": "/usr/bin", "HERMES_HOME": SENDER_HOME, "HERMES_PROFILE": "peer-lane"}
    env = bot_mode_dm._delivery_turn_env(base)
    assert "HERMES_HOME" not in env and "HERMES_PROFILE" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HERMES_DELIVERY_TURN"] == "1"


def test_delivery_turn_env_does_not_mutate_its_base():
    base = {"PATH": "/usr/bin", "HERMES_HOME": SENDER_HOME}
    bot_mode_dm._delivery_turn_env(base)
    assert base["HERMES_HOME"] == SENDER_HOME


def test_relay_delivery_env_drops_identity_but_keeps_the_author(monkeypatch):
    _seed_sender_identity(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_ID", "grandparent-session")
    author = {"id": "bot:researcher", "name": "researcher", "is_bot": True}

    env = bot_relay.delivery_env(author)

    for name in IDENTITY:
        assert name not in env
    assert env.get("HERMES_SESSION_ID") != "grandparent-session"
    assert env["PATH"] == "/usr/bin"
    from agent.turn_author import TURN_AUTHOR_ENV

    assert env.get(TURN_AUTHOR_ENV)


def test_relay_delivery_env_without_an_author_is_a_fresh_child(monkeypatch):
    _seed_sender_identity(monkeypatch)
    env = bot_relay.delivery_env(None)
    for name in IDENTITY:
        assert name not in env
    from agent.turn_author import TURN_AUTHOR_ENV

    assert TURN_AUTHOR_ENV not in env


def test_the_scrub_does_not_touch_unrelated_hermes_settings(monkeypatch):
    """Only identity is scrubbed: a delivery child still needs the sender's knobs (timeouts, board)."""
    _seed_sender_identity(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "ops")
    monkeypatch.setenv("HERMES_TURN_LEASE_TIMEOUT", "5")
    env = bot_mode_dm._delivery_turn_env()
    assert env["HERMES_KANBAN_BOARD"] == "ops"
    assert env["HERMES_TURN_LEASE_TIMEOUT"] == "5"
