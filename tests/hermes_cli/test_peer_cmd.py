"""Tests for ``hermes peer`` — cross-machine bot-to-bot DMs."""

import argparse
import json
import threading
import time
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import hermes_yaml
import pytest

from agent.turn_author import TURN_AUTHOR_ENV
from hermes_cli.subcommands import peer as peer_cmd


# ── target parsing ───────────────────────────────────────────────────────────


def test_parse_target_bare_peer():
    assert peer_cmd._parse_target("spark") == ("spark", None)


def test_parse_target_peer_and_profile():
    assert peer_cmd._parse_target("spark/researcher") == ("spark", "researcher")


def test_parse_target_rejects_empty():
    with pytest.raises(ValueError):
        peer_cmd._parse_target("")


def test_parse_target_rejects_bad_profile():
    with pytest.raises(ValueError):
        peer_cmd._parse_target("spark/../etc")


# ── url scoping ──────────────────────────────────────────────────────────────


def test_base_url_bare_and_profile():
    peer = {"url": "http://spark.lan:8377/"}
    assert peer_cmd._base_url(peer, None) == "http://spark.lan:8377"
    assert peer_cmd._base_url(peer, "researcher") == "http://spark.lan:8377/p/researcher"


# ── registry round-trip (isolated config) ────────────────────────────────────


def test_add_list_remove_roundtrip(monkeypatch, capsys):
    store = {}

    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: dict(store))

    def fake_save(peers):
        store.clear()
        store.update(peers)

    monkeypatch.setattr(peer_cmd, "_save_peers", fake_save)
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k" * 20)

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(peer_action="add", name="spark", url="http://spark.lan:8377", key="", note="")
    )
    assert rc == 0
    assert store["spark"]["url"] == "http://spark.lan:8377"

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="list"))
    assert rc == 0
    assert "spark" in capsys.readouterr().out

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="remove", name="spark"))
    assert rc == 0
    assert "spark" not in store


def test_add_rejects_bad_name_and_url(monkeypatch):
    monkeypatch.setattr(peer_cmd, "_load_peers", dict)
    monkeypatch.setattr(peer_cmd, "_save_peers", lambda peers: None)

    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="add", name="Bad Name!", url="http://x", key="", note="")) == 2
    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="add", name="ok", url="ftp://x", key="", note="")) == 2


def test_dm_unknown_peer_and_missing_key(monkeypatch):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")

    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="nope", message="hi", json=False)) == 1
    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="hi", json=False)) == 1


# ── live HTTP dm flow (real loopback server, fake peer gateway) ──────────────


class _FakePeer(BaseHTTPRequestHandler):
    sessions: list = []
    chats: list = []
    chat_bodies: list = []
    runs: list = []
    run_idempotency_keys: list = []
    auth_seen: list = []
    chat_reply_content: str = "reply from the other machine"

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path == "/v1/capabilities":
            return self._json(
                {
                    "features": {
                        "runs_idempotency": {
                            "supported": True,
                            "durable": True,
                            "retention_seconds": 86400,
                        }
                    }
                }
            )
        if self.path == "/v1/runs/run_1":
            return self._json({
                "object": "hermes.run",
                "run_id": "run_1",
                "status": "completed",
                "session_id": "bc_existing",
                "output": "async reply from the other machine",
            })
        if self.path.startswith("/api/sessions"):
            data = [{"id": s, "title": "Bot Chat"} for s in type(self).sessions]
            return self._json({"object": "list", "data": data})
        return self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/api/sessions":
            type(self).sessions.append("bc_1")
            # REAL api_server create shape: the row is wrapped under "session"
            # (verified live Aug 2026 — a flat fake hid a parser bug).
            return self._json({"object": "hermes.session", "session": {"id": "bc_1", "title": body.get("title")}}, 201)

        if self.path.startswith("/api/sessions/") and self.path.endswith("/chat"):
            type(self).chats.append(body.get("message"))
            type(self).chat_bodies.append(body)
            return self._json({
                "object": "hermes.session.chat.completion",
                "session_id": "bc_1",
                "message": {
                    "role": "assistant",
                    "content": type(self).chat_reply_content,
                },
            })

        if self.path == "/v1/runs":
            type(self).runs.append(body)
            type(self).run_idempotency_keys.append(
                self.headers.get("Idempotency-Key", "")
            )
            return self._json(
                {"run_id": "run_1", "status": "started", "replayed": False},
                202,
            )

        if self.path == "/v1/runs/run_1/stop":
            return self._json({"run_id": "run_1", "status": "stopping"})

        return self._json({"error": {"message": "not found"}}, 404)

    def log_message(self, *args):
        pass


@pytest.fixture()
def fake_peer_server():
    _FakePeer.sessions = []
    _FakePeer.chats = []
    _FakePeer.chat_bodies = []
    _FakePeer.runs = []
    _FakePeer.run_idempotency_keys = []
    _FakePeer.auth_seen = []
    _FakePeer.chat_reply_content = "reply from the other machine"
    server = HTTPServer(("127.0.0.1", 0), _FakePeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_dm_creates_bot_chat_then_chats(monkeypatch, capsys, fake_peer_server):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="dm",
            target="spark",
            message="Message from 🤖 dixie (@dixie): disk status?",
            json=False,
        )
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "reply from the other machine" in out
    # One Bot Chat was created (none existed), then the chat turn ran on it.
    assert _FakePeer.sessions == ["bc_1"]
    assert _FakePeer.chats == ["Message from 🤖 dixie (@dixie): disk status?"]
    # Every request carried the peer key.
    assert all(a == "Bearer secret-key-123456" for a in _FakePeer.auth_seen)


def test_dm_hides_a_bare_silence_marker_from_the_sending_agent(monkeypatch, capsys, fake_peer_server):
    """A successful bare NO_REPLY/[SILENT] marker is a delivery decision, not
    a message — same rule as the gateway's live Bot Chat completion, the
    Desktop bot_relay.deliver RPC and the one-shot local `hermes chat -Q`
    transport (tools/bot_mode_dm.py::_run_local_turn). `hermes peer dm` is a
    4th Bot Mode delivery door and, before this fix, printed the raw marker
    to the sending agent as if it were a real reply."""
    _FakePeer.chat_reply_content = "NO_REPLY"
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(peer_action="dm", target="spark", message="ping", json=False)
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "NO_REPLY" not in out
    assert "(no reply)" in out


def test_dm_json_output_hides_a_bare_silence_marker_too(monkeypatch, capsys, fake_peer_server):
    _FakePeer.chat_reply_content = "[SILENT]"
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True)
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == ""


def test_dm_delivers_prose_that_only_mentions_the_marker_normally(monkeypatch, capsys, fake_peer_server):
    """Prose that merely mentions NO_REPLY must still be delivered — only an
    EXACT whole-response marker is silence."""
    _FakePeer.chat_reply_content = "The status is NO_REPLY needed right now, all clear."
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(peer_action="dm", target="spark", message="ping", json=False)
    )

    assert rc == 0
    assert "The status is NO_REPLY needed right now, all clear." in capsys.readouterr().out


def test_dm_reuses_existing_bot_chat(monkeypatch, capsys, fake_peer_server):
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == "reply from the other machine"
    # No new session was created — the existing canonical chat was reused.
    assert _FakePeer.sessions == ["bc_existing"]


# ── per-turn author (HERMES_TURN_AUTHOR set by the message_agent runner) ─────


AUTHOR = {"id": "bot:dixie", "name": "dixie", "is_bot": True}


def _peer_spark(monkeypatch, url, author_env):
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")
    if author_env is None:
        monkeypatch.delenv(TURN_AUTHOR_ENV, raising=False)
    else:
        monkeypatch.setenv(TURN_AUTHOR_ENV, json.dumps(author_env))


@pytest.mark.parametrize("author_env, expected_body", [
    ({**AUTHOR, "x": 1}, {"message": "ping", "author": AUTHOR}),
    (None, {"message": "ping"}),
], ids=["author from env", "no env"])
def test_dm_body_carries_author_only_from_env(monkeypatch, fake_peer_server, author_env, expected_body):
    _peer_spark(monkeypatch, fake_peer_server, author_env)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True))

    assert rc == 0
    assert _FakePeer.chat_bodies == [expected_body]


def test_run_body_carries_author_from_env(monkeypatch, capsys, fake_peer_server):
    _peer_spark(monkeypatch, fake_peer_server, AUTHOR)

    rc = peer_cmd.cmd_peer(SimpleNamespace(
        peer_action="run", target="spark", message="long task", idempotency_key="ticket-1", json=True))

    assert rc == 0
    assert _FakePeer.runs == [{"input": "long task", "session_id": "bc_existing", "author": AUTHOR}]


# ── hidden canonical Bot Chat (issue #91583) ─────────────────────────────────


class _HiddenBotChatPeer(_FakePeer):
    """A NEW-style peer: its Bot Chat exists but is HIDDEN (Bot Mode hides
    canonical chats), so it only appears in the listing when the client
    sends the exact-title + include_hidden lookup."""

    hidden_sessions: list = []
    get_queries: list = []

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path.startswith("/api/sessions"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            type(self).get_queries.append(query)
            data = [{"id": s, "title": "Bot Chat", "hidden": False} for s in type(self).sessions]
            if query.get("title", [""])[0] == "Bot Chat" and query.get("include_hidden", ["0"])[0] in ("1", "true"):
                data += [{"id": s, "title": "Bot Chat", "hidden": True} for s in type(self).hidden_sessions]
            return self._json({"object": "list", "data": data})
        return self._json({"error": {"message": "not found"}}, 404)


class _OldHiddenBotChatPeer(_FakePeer):
    """An OLD peer: ignores title/include_hidden, its hidden Bot Chat is
    invisible in every listing, and the duplicate create trips the DB's
    UNIQUE(title) guard with the real api_server 400 shape."""

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path.startswith("/api/sessions"):
            return self._json({"object": "list", "data": []})
        return self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if self.path == "/api/sessions":
            return self._json(
                {"error": {"message": "Title already in use by session hidden_bc_1", "code": "invalid_title"}},
                400,
            )
        return self._json({"error": {"message": "not found"}}, 404)


@pytest.fixture()
def hidden_peer_server():
    _HiddenBotChatPeer.sessions = []
    _HiddenBotChatPeer.hidden_sessions = ["bc_hidden"]
    _HiddenBotChatPeer.chats = []
    _HiddenBotChatPeer.auth_seen = []
    _HiddenBotChatPeer.get_queries = []
    server = HTTPServer(("127.0.0.1", 0), _HiddenBotChatPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def old_hidden_peer_server():
    _OldHiddenBotChatPeer.sessions = []
    _OldHiddenBotChatPeer.chats = []
    _OldHiddenBotChatPeer.auth_seen = []
    server = HTTPServer(("127.0.0.1", 0), _OldHiddenBotChatPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_find_bot_chat_sends_hidden_aware_lookup(hidden_peer_server):
    """The lookup carries title + include_hidden so a hidden canonical row resolves."""
    found = peer_cmd._find_bot_chat(hidden_peer_server, "secret-key-123456")
    assert found == "bc_hidden"
    query = _HiddenBotChatPeer.get_queries[-1]
    assert query.get("title") == ["Bot Chat"]
    assert query.get("include_hidden") == ["1"]


def test_dm_resolves_hidden_bot_chat_without_duplicate_create(monkeypatch, capsys, hidden_peer_server):
    """Regression for issue #91583: hidden canonical Bot Chat must be reused,
    never re-created (the peer's UNIQUE(title) guard rejects the duplicate)."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": hidden_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == "reply from the other machine"
    # No create was attempted: the hidden canonical chat resolved directly.
    assert _HiddenBotChatPeer.sessions == []
    assert _HiddenBotChatPeer.chats == ["ping"]


def test_dm_older_peer_hidden_duplicate_gives_clear_error(monkeypatch, capsys, old_hidden_peer_server):
    """Against an older peer that can't expose hidden sessions, the UNIQUE(title)
    rejection must surface a diagnosable error naming the hidden canonical chat."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": old_hidden_peer_server}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="ping", json=False))

    assert rc == 1
    err = capsys.readouterr().err
    assert "hidden" in err
    assert "Bot Chat" in err
    assert "Title already in use" in err




def test_run_starts_async_turn_with_canonical_session_and_idempotency(
    monkeypatch, capsys, fake_peer_server
):
    _FakePeer.sessions = ["bc_existing"]
    monkeypatch.setattr(
        peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}}
    )
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="run",
            target="spark",
            message="long task",
            idempotency_key="ticket-123",
            json=True,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "peer": "spark",
        "profile": None,
        "session_id": "bc_existing",
        "run_id": "run_1",
        "status": "started",
        "idempotency_key": "ticket-123",
        "replayed": False,
    }
    assert _FakePeer.runs == [{"input": "long task", "session_id": "bc_existing"}]
    assert _FakePeer.run_idempotency_keys == ["ticket-123"]


def test_status_reads_async_run_output(monkeypatch, capsys, fake_peer_server):
    monkeypatch.setattr(
        peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}}
    )
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="status",
            target="spark",
            run_id="run_1",
            json=True,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert payload["output"] == "async reply from the other machine"


def test_stop_requests_exact_async_run(monkeypatch, capsys, fake_peer_server):
    monkeypatch.setattr(
        peer_cmd, "_load_peers", lambda: {"spark": {"url": fake_peer_server}}
    )
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")

    rc = peer_cmd.cmd_peer(
        SimpleNamespace(
            peer_action="stop",
            target="spark",
            run_id="run_1",
            json=True,
        )
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run_1"
    assert payload["status"] == "stopping"


# ── cross-origin redirect must not carry the peer's Bearer key ──────────────


class _AttackerOrigin(BaseHTTPRequestHandler):
    """A second real HTTP server standing in for an attacker-controlled host
    a compromised/MITM'd peer could redirect a ``hermes peer dm`` request to."""

    auth_seen: list = []

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization"))
        body = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _RedirectingPeer(BaseHTTPRequestHandler):
    """A "peer" that 302-redirects every request to a different origin —
    the shape of a compromised peer or a LAN MITM answering ``hermes peer
    add``'s registered URL."""

    redirect_target: str = ""

    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", type(self).redirect_target + self.path)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_request_strips_bearer_key_across_redirect_origin():
    """``_request`` must not forward the peer's Authorization: Bearer key to
    a different origin a redirect points at (compromised peer / LAN MITM) —
    the exact class of leak ``open_credentialed_url`` exists to close."""
    _AttackerOrigin.auth_seen = []
    attacker = HTTPServer(("127.0.0.1", 0), _AttackerOrigin)
    attacker_thread = threading.Thread(target=attacker.serve_forever, daemon=True)
    attacker_thread.start()

    _RedirectingPeer.redirect_target = f"http://127.0.0.1:{attacker.server_port}"
    peer = HTTPServer(("127.0.0.1", 0), _RedirectingPeer)
    peer_thread = threading.Thread(target=peer.serve_forever, daemon=True)
    peer_thread.start()

    try:
        # The attacker origin answers with a well-formed (empty) listing, so
        # the redirect completes successfully — the request itself is not
        # the point of this test, only whether the Bearer key rode along.
        result = peer_cmd._request(f"http://127.0.0.1:{peer.server_port}/api/sessions", "top-secret-peer-key")
        assert result == {"object": "list", "data": []}
    finally:
        peer.shutdown()
        peer_thread.join(timeout=5)
        attacker.shutdown()
        attacker_thread.join(timeout=5)

    assert _AttackerOrigin.auth_seen, "redirect target was never reached"
    assert all(header is None for header in _AttackerOrigin.auth_seen), (
        f"peer's Bearer key leaked to the redirect target: {_AttackerOrigin.auth_seen}"
    )


# ── the registry's HOME (measured 2026-10-13) ─────────────────────────────────
#
# The registry lived in the ROOT ~/.hermes/config.yaml only, while a profile-scoped session
# resolves load_config() against its OWN home: every lane's `hermes peer dm` died with
# `No peer named 'yoyodine'`, and the one profile that had been re-registered by hand worked
# alone. The registry is a host-level fact - one home, the root - never a copy per profile.

ROOT_PEERS = {"yoyodine": {"url": "http://127.0.0.1:8644"}}


@pytest.fixture
def farm(tmp_path, monkeypatch):
    """The root ~/.hermes (the registry's home) plus one named profile under it, with
    Path.home() and HERMES_HOME pointed into the fixture the way a profile session runs."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "platform-coder"
    profile.mkdir(parents=True)
    (root / "config.yaml").write_text(
        "bot_peers:\n  yoyodine:\n    url: http://127.0.0.1:8644\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return root, profile


def test_a_profile_session_resolves_the_root_registry(farm, monkeypatch, capsys):
    """(a) The review gate: `hermes peer list` resolves the peer from a profile session whose
    own config declares no `bot_peers`."""
    root, profile = farm
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert peer_cmd._registry_home() == root
    assert peer_cmd._load_peers() == ROOT_PEERS
    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="list")) == 0
    assert "yoyodine" in capsys.readouterr().out


def test_a_profile_that_declares_its_own_registry_owns_it(farm, monkeypatch):
    """A profile pointing at its OWN gateway keeps it: the fallback must not override a
    deliberate per-profile registry."""
    _root, profile = farm
    (profile / "config.yaml").write_text(
        "bot_peers:\n  homelab:\n    url: http://10.0.0.9:8377\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))

    assert peer_cmd._registry_home() == profile
    assert peer_cmd._load_peers() == {"homelab": {"url": "http://10.0.0.9:8377"}}


def test_the_default_home_resolves_its_own_registry(farm, monkeypatch):
    root, _profile = farm
    monkeypatch.setenv("HERMES_HOME", str(root))

    assert peer_cmd._registry_home() == root
    assert peer_cmd._load_peers() == ROOT_PEERS


def test_peer_add_from_a_profile_writes_the_root_registry(farm, monkeypatch):
    """`hermes peer add` must not fork the registry into a profile copy - the write lands in
    the home that OWNS it (one fact, one home)."""
    root, profile = farm
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k" * 20)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="add", name="homelab",
                                           url="http://10.0.0.9:8377", key="", note=""))

    assert rc == 0
    written = hermes_yaml.safe_load((root / "config.yaml").read_text())
    assert set(written["bot_peers"]) == {"yoyodine", "homelab"}
    assert not (profile / "config.yaml").exists(), "the registry was forked into a profile copy"


# ── a REFUSAL must land where a human reads it ────────────────────────────────
#
# Measured cost: a fleet-wide DM outage reached only callers' stderr and nobody noticed until
# the operator asked why DMs were broken. A refusal that means "the DM path is broken" now
# files ONE idempotency-keyed card on the ops board.


def _recording_filer(calls, card_id="t_card1234"):
    def filer(**kwargs):
        calls.append(kwargs)
        return card_id
    return filer


def test_a_no_peer_refusal_files_one_card_on_the_ops_board(monkeypatch, capsys):
    """(c) The review gate: one DM failure files a card, and the caller still exits 1."""
    calls = []
    monkeypatch.setattr(peer_cmd, "_create_card", _recording_filer(calls))
    monkeypatch.setattr(peer_cmd, "_load_peers", dict)          # EMPTY registry: the outage

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="yoyodine/default",
                                           message="hi", json=False))

    assert rc == 1
    assert len(calls) == 1
    call = calls[0]
    assert call["board"] == peer_cmd.REFUSAL_BOARD
    assert call["assignee"] == peer_cmd.REFUSAL_ASSIGNEE
    assert call["idempotency_key"] == "peer-refusal-yoyodine-no-peer"
    assert "yoyodine" in call["title"] and "no-peer" in call["title"]
    assert "no-peer" in call["body"]
    err = capsys.readouterr().err
    assert "Reported:" in err and "t_card1234" in err


def test_a_missing_key_refusal_files_a_card_keyed_by_class(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(peer_cmd, "_create_card", _recording_filer(calls))
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"yoyodine": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "")

    assert peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="yoyodine", message="hi",
                                             json=False)) == 1

    assert calls[0]["idempotency_key"] == "peer-refusal-yoyodine-no-key"


def test_a_capacity_refusal_files_a_card_keyed_by_status(monkeypatch, capsys):
    """The 429 the peer's own cap returns (`Too many concurrent runs`) is a control-channel
    starvation, not a transport blip: it is reported."""
    calls = []
    monkeypatch.setattr(peer_cmd, "_create_card", _recording_filer(calls))
    err = urllib.error.HTTPError("http://peer/v1/runs", 429, "Too many concurrent runs",
                                 Message(), None)

    assert peer_cmd._peer_failure("yoyodine", err) == 1

    assert calls[0]["idempotency_key"] == "peer-refusal-yoyodine-http-429"
    assert "429" in calls[0]["body"]
    assert "Too many concurrent runs" in calls[0]["body"]


def test_a_transport_failure_files_no_card(monkeypatch, capsys):
    """A peer that is briefly unreachable is the reachability probe's business: a card per
    blip is noise, so the reported classes are the DETERMINISTIC refusals only."""
    calls = []
    monkeypatch.setattr(peer_cmd, "_create_card", _recording_filer(calls))

    assert peer_cmd._peer_failure("yoyodine", urllib.error.URLError("connection refused")) == 1

    assert calls == []
    assert "Could not reach peer" in capsys.readouterr().err


def test_a_board_write_failure_never_breaks_the_refusal(monkeypatch, capsys):
    """Reporting is a side effect of failing: it must never turn one failure into two.

    And the reason it prints must be the REAL one -- 'the board write failed' used to be
    printed for a fenced-context refusal, which sent the reader after the wrong problem.
    """
    def boom(**kwargs):
        raise RuntimeError("the board is locked")

    monkeypatch.setattr(peer_cmd, "_create_card", boom)
    monkeypatch.setattr(peer_cmd, "_load_peers", dict)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="yoyodine", message="hi",
                                           json=False))

    assert rc == 1
    err = capsys.readouterr().err
    assert "NOT FILED" in err and "RuntimeError: the board is locked" in err


def test_the_idempotency_hit_is_not_reported_as_a_failure(monkeypatch, capsys):
    """Two outcomes, two lines: 'already filed' carries the SAME id and files no second row."""
    calls = []
    monkeypatch.setattr(peer_cmd, "_create_card", _recording_filer(calls, "t_seen0001"))
    monkeypatch.setattr(peer_cmd, "_existing_refusal_card", lambda board, key: "")

    first = peer_cmd._report_refusal("yoyodine", "no-peer", "detail")
    assert first == "t_seen0001"
    assert "filed: card t_seen0001" in capsys.readouterr().err

    monkeypatch.setattr(peer_cmd, "_existing_refusal_card", lambda board, key: "t_seen0001")
    second = peer_cmd._report_refusal("yoyodine", "no-peer", "detail")
    err = capsys.readouterr().err
    assert second == "t_seen0001"
    assert "already filed: card t_seen0001" in err
    assert "NOT FILED" not in err
    assert len(calls) == 1, "the idempotency hit filed a second row"


def test_the_refusal_write_runs_under_the_reviewed_child_context_allowance(monkeypatch):
    """The fence admits exactly ONE named purpose, and the report sets it around the write."""
    from agent import delegation_context as dc

    seen = []

    def filer(**kwargs):
        seen.append(dc.child_kanban_write_purpose())
        return "t_allowed1"

    monkeypatch.setattr(peer_cmd, "_create_card", filer)
    card, reason = peer_cmd._file_refusal_card(
        "yoyodine", "no-peer", "detail",
        target={"board": "default", "assignee": "platform-stl", "priority": 3})

    assert (card, reason) == ("t_allowed1", "")
    assert seen == [peer_cmd.REFUSAL_WRITE_PURPOSE]
    assert dc.child_kanban_write_purpose() == "", "the allowance leaked past the write"


def test_the_child_write_allowance_is_closed_outside_its_named_purpose(tmp_path, monkeypatch):
    import sqlite3

    from agent import delegation_context as dc
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(tmp_path))

    with pytest.raises(ValueError):
        with dc.child_kanban_write_allowance("because-i-want-to"):
            pass

    (tmp_path / "kanban").mkdir()
    conn = sqlite3.connect(str(tmp_path / "kanban" / "kanban.db"))
    try:
        with pytest.raises(PermissionError):
            with kbc.write_txn(conn):
                pass
        with pytest.raises(PermissionError):
            kb._assert_not_delegated_child_mutation(tmp_path / "kanban" / "kanban.db")

        with dc.child_kanban_write_allowance(dc.CHILD_KANBAN_WRITE_PURPOSES[0]):
            with kbc.write_txn(conn):
                pass
    finally:
        conn.close()


def test_a_dispatcher_spawned_worker_files_the_refusal_card_and_the_repeat_is_a_hit(
        tmp_path, monkeypatch, capsys):
    """The D11 acceptance: the report ACTS from a fenced context, and the second refusal of
    the same (peer, class) prints the SAME card id and writes no second row."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    root = tmp_path / "home"
    (root / "profiles" / "platform-stl").mkdir(parents=True)
    (root / "profiles" / "platform-stl" / "config.yaml").write_text("model: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(peer_cmd, "_refusal_target",
                        lambda: {"board": "default", "assignee": "platform-stl", "priority": 3})

    with kbc.connect_closing(board="default"):     # the owner initializes the store
        pass
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(root))   # now: a worker

    first = peer_cmd._report_refusal("yoyodine", "no-peer", "detail")
    assert first.startswith("t_"), capsys.readouterr().err
    assert f"filed: card {first}" in capsys.readouterr().err

    second = peer_cmd._report_refusal("yoyodine", "no-peer", "detail")
    err = capsys.readouterr().err
    assert second == first, err
    assert f"already filed: card {first}" in err

    with kbc.connect_closing(board="default") as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?",
            (peer_cmd._refusal_key("yoyodine", "no-peer"),)).fetchone()[0]
    assert rows == 1


def test_the_refusal_target_is_configurable(farm, monkeypatch):
    """The fleet's own board/owner for these reports is config, not a code change."""
    root, _profile = farm
    (root / "config.yaml").write_text(
        "bot_peers:\n  yoyodine:\n    url: http://127.0.0.1:8644\n"
        "bot_peer_refusals:\n  board: platform\n  assignee: platform-worker\n  priority: 7\n")
    monkeypatch.setenv("HERMES_HOME", str(root))

    assert peer_cmd._refusal_target() == {"board": "platform", "assignee": "platform-worker",
                                          "priority": 7}
# ── T6: a post-retention 404 on the read path is not a failure ───────────────
# (live loopback server: the real urllib HTTPError the evidence showed)


def _peer_for(monkeypatch, url):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")


def test_status_404_after_retention_is_not_a_failure(monkeypatch, capsys, fake_peer_server):
    """Spec T6: terminal run rows are GC'd after RETENTION_SECONDS, so a 404
    means the record is gone — never that the delivery failed. Exit 0."""
    _peer_for(monkeypatch, fake_peer_server)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="status", target="spark",
                                           run_id="run_gone", json=False))

    assert rc == 0
    out = capsys.readouterr().out
    assert "run_gone" in out
    assert "not a delivery failure" in out


def test_status_404_json_emits_an_unknown_envelope(monkeypatch, capsys, fake_peer_server):
    _peer_for(monkeypatch, fake_peer_server)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="status", target="spark",
                                           run_id="run_gone", json=True))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["object"] == "hermes.peer.run"
    assert payload["result"] == "unknown"
    assert payload["status"] == "unknown"
    assert payload["run_id"] == "run_gone"
    assert payload["reason"] == "run_not_found"
    assert payload["retryable"] is False


def test_stop_404_is_unchanged(monkeypatch, capsys, fake_peer_server):
    """The carve-out is the READ path only: ``peer stop`` keeps exit 1 + message."""
    _peer_for(monkeypatch, fake_peer_server)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="stop", target="spark",
                                           run_id="run_gone", json=False))

    assert rc == 1
    assert "HTTP 404" in capsys.readouterr().err


class _ServerErrorPeer(_FakePeer):
    """A peer whose run-read fails with HTTP 500 — guards the 404 carve-out."""

    def do_GET(self):
        type(self).auth_seen.append(self.headers.get("Authorization", ""))
        if self.path.startswith("/v1/runs/"):
            return self._json({"error": {"message": "internal error"}}, 500)
        return super().do_GET()


@pytest.fixture()
def server_error_peer_server():
    _ServerErrorPeer.sessions = []
    _ServerErrorPeer.chats = []
    _ServerErrorPeer.auth_seen = []
    server = HTTPServer(("127.0.0.1", 0), _ServerErrorPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_status_500_still_exits_nonzero(monkeypatch, capsys, server_error_peer_server):
    """Every other HTTP status keeps today's behaviour exactly."""
    _peer_for(monkeypatch, server_error_peer_server)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="status", target="spark",
                                           run_id="run_1", json=False))

    assert rc == 1
    captured = capsys.readouterr()
    assert "HTTP 500" in captured.err
    assert captured.out == ""


# ── D3 over a REAL socket (no transport stubbing) ────────────────────────────


class _SlowDeliveryPeer(BaseHTTPRequestHandler):
    """A peer whose first delivery POST goes mid-turn and never answers.

    Reproduces the D3 evidence exactly: the response *headers* arrive, the body
    never does, so the client's read timeout fires inside ``resp.read()`` as a
    bare ``socket.timeout`` — not a wrapped ``URLError``.
    """

    posts: list = []          # (lowercased headers, body) per delivery POST
    hang_seconds = 2.5

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/sessions"):
            return self._json({"object": "list", "data": [{"id": "bc_1", "title": "Bot Chat"}]})
        return self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if not (self.path.startswith("/api/sessions/") and self.path.endswith("/chat")):
            return self._json({"error": {"message": "not found"}}, 404)
        headers = {k.lower(): v for k, v in self.headers.items()}
        type(self).posts.append((headers, body))
        if len(type(self).posts) == 1:
            # Accepted and reserved, still executing: headers now, body never.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "4000")
            self.end_headers()
            time.sleep(type(self).hang_seconds)
            self.close_connection = True
            return
        # The replay hit the idempotent reservation: same key, no second turn.
        return self._json({
            "object": "hermes.peer.send_result",
            "result": "receipt",
            "status": "accepted",
            "replayed": True,
            "session_id": "bc_1",
            "run_id": "run_77",
            "idempotency_key": headers.get("idempotency-key", ""),
            "retryable": False,
        })

    def log_message(self, *args):  # silence test server logging
        pass


@pytest.fixture()
def slow_delivery_peer_server():
    _SlowDeliveryPeer.posts = []
    _SlowDeliveryPeer.hang_seconds = 2.5
    # Single-threaded ``HTTPServer`` would serialise the replay behind the
    # hanging first request; the gateway is concurrent, so the fake must be too.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowDeliveryPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_dm_read_timeout_over_a_real_socket_hands_back_the_handle(monkeypatch, capsys, slow_delivery_peer_server):
    """A real mid-body read timeout, under the ceiling contract.

    The socket timeout IS this call's ceiling, so a real read timeout has spent it: there is no
    budget left for the idempotent replay, and the client must not spend another 30 s nobody
    budgeted for (the old behaviour the ceiling removes). What it must do instead is report the
    delivery it knows about — ONE turn, exit 3, "accepted ... Do NOT resend", and the
    `hermes peer reply` handle — and never call the peer unreachable.

    (The replay itself is proven by the mocked tests in test_peer_receipt_client.py, where the
    response is lost before the ceiling is spent.)
    """
    _peer_for(monkeypatch, slow_delivery_peer_server)
    # Shrink only the HTTP headroom so the real read timeout fires in ~1.3s.
    monkeypatch.setattr(peer_cmd, "_DM_TIMEOUT_SLACK_S", 0.3)
    monkeypatch.setattr(peer_cmd, "CONFIRM_READ_INTERVAL_S", 0)

    rc = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark",
                                           message="status?", wait_seconds=1, json=True))

    captured = capsys.readouterr()
    assert rc == 3, captured.err
    # Exactly ONE delivery POST: the ceiling leaves no room for the replay.
    assert len(_SlowDeliveryPeer.posts) == 1, "the client re-POSTed past its own ceiling"
    headers, body = _SlowDeliveryPeer.posts[0]
    assert body == {"message": "status?"}
    assert headers["x-hermes-wait-seconds"] == "1"
    payload = json.loads(captured.out)
    assert payload["status"] == "pending"
    assert payload["delivery"] == "accepted"
    assert payload["session_id"] == "bc_1"
    assert payload["elapsed_s"] <= 2.0
    text = (captured.out + captured.err).lower()
    assert "unreachable" not in text
    assert "Could not reach peer" not in captured.err


# ── `dm --timeout` / `--no-wait` (caller-bounded wait) ───────────────────────


def _peer_parser():
    """The real ``hermes peer`` parser, exactly as ``hermes_cli.main`` attaches it."""
    root = argparse.ArgumentParser(prog="hermes")
    subs = root.add_subparsers(dest="command")
    peer_cmd.build_peer_parser(subs)
    return root


def test_dm_parser_takes_timeout_and_no_wait():
    parser = _peer_parser()

    args = parser.parse_args(["peer", "dm", "spark", "hi", "--timeout", "30"])
    assert (args.peer_action, args.target, args.message) == ("dm", "spark", "hi")
    assert (args.timeout, args.no_wait) == (30, False)

    args = parser.parse_args(["peer", "dm", "spark", "hi", "--no-wait"])
    assert (args.timeout, args.no_wait) == (None, True)


@pytest.mark.parametrize("bad", [["--timeout", "1.5"], ["--timeout", "soon"], ["--timeout", ""]])
def test_dm_parser_rejects_a_non_integer_timeout(bad):
    with pytest.raises(SystemExit) as excinfo:
        _peer_parser().parse_args(["peer", "dm", "spark", "hi", *bad])
    assert excinfo.value.code == 2


def test_dm_parser_rejects_timeout_together_with_no_wait():
    with pytest.raises(SystemExit) as excinfo:
        _peer_parser().parse_args(["peer", "dm", "spark", "hi", "--timeout", "30", "--no-wait"])
    assert excinfo.value.code == 2


def test_dm_parser_rejects_wait_together_with_no_wait():
    """`--wait` is the budget asked OF the peer; `--no-wait` never waits for its reply."""
    with pytest.raises(SystemExit) as excinfo:
        _peer_parser().parse_args(["peer", "dm", "spark", "hi", "--wait", "30", "--no-wait"])
    assert excinfo.value.code == 2


@pytest.mark.parametrize("value", [0, -1, "0", "-1"])
def test_dm_rejects_a_timeout_below_one_second(monkeypatch, capsys, value):
    """There is deliberately no ``--timeout 0`` alias for ``--no-wait``: a bound under a second is
    a typo, not a request."""
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k" * 12)

    code = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="hi",
                                             json=False, timeout=value, no_wait=False))

    assert code == 2
    assert "--timeout" in capsys.readouterr().err


def test_dm_timeout_and_no_wait_together_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": "http://x"}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "k" * 12)

    code = peer_cmd.cmd_peer(SimpleNamespace(peer_action="dm", target="spark", message="hi",
                                             json=False, timeout=30, no_wait=True))

    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


# ── `hermes peer reply` — collect a reply the dm could not wait for ──────────


class _RepliesPeer(BaseHTTPRequestHandler):
    """A read-only peer: sessions listing + scripted message snapshots, and a POST counter.

    ``snapshots`` is a list of transcript pages; each read advances to the next and the last one
    repeats, so ``--wait`` polling is testable.
    """

    sessions: list = []
    snapshots: list = []
    reads: int = 0
    gets: list = []
    posts: list = []
    messages_status: int = 200

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        type(self).gets.append(self.path)
        if "/messages" in self.path:
            if type(self).messages_status != 200:
                return self._json({"error": {"message": "Session not found: gone"}},
                                  type(self).messages_status)
            index = min(type(self).reads, len(type(self).snapshots) - 1)
            type(self).reads += 1
            rows = [dict(row) for row in type(self).snapshots[index]] if type(self).snapshots else []
            return self._json({"object": "list", "session_id": "bc_1", "data": rows})
        if self.path.startswith("/api/sessions"):
            data = [{"id": s, "title": "Bot Chat"} for s in type(self).sessions]
            return self._json({"object": "list", "data": data})
        return self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        # A read verb must never write; the tests assert this stays empty.
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        type(self).posts.append(self.path)
        return self._json({"error": {"message": "not found"}}, 404)

    def log_message(self, *args):
        pass


@pytest.fixture()
def replies_peer():
    _RepliesPeer.sessions = ["bc_1"]
    _RepliesPeer.snapshots = [[]]
    _RepliesPeer.reads = 0
    _RepliesPeer.gets = []
    _RepliesPeer.posts = []
    _RepliesPeer.messages_status = 200
    server = HTTPServer(("127.0.0.1", 0), _RepliesPeer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _reply(monkeypatch, url, *, session=None, after=None, wait=0, json_mode=False, target="spark"):
    monkeypatch.setattr(peer_cmd, "_load_peers", lambda: {"spark": {"url": url}})
    monkeypatch.setattr(peer_cmd, "_peer_secret", lambda name: "secret-key-123456")
    return peer_cmd.cmd_peer(SimpleNamespace(peer_action="reply", target=target, session=session,
                                            after=after, wait=wait, json=json_mode))


def test_reply_prints_the_answered_text_alone(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "ping"},
                               {"id": 2, "role": "assistant", "content": "pong"}]]

    code = _reply(monkeypatch, replies_peer)
    out = capsys.readouterr().out

    assert code == 0
    assert out == "pong\n"                      # parity with `dm` so callers reuse their parsing
    assert _RepliesPeer.posts == []             # a read never writes


def test_reply_reports_attribution_and_the_tail_in_json(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "x" * 500},
                               {"id": 2, "role": "assistant", "content": "pong"}]]

    code = _reply(monkeypatch, replies_peer, json_mode=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["status"] == "completed"
    assert payload["reply"] == "pong"
    assert payload["reply_to"] == "x" * 200     # truncated for attribution
    assert payload["message_id"] == 2
    assert payload["rows_seen"] == 2
    assert payload["after"] is None             # no cursor: the transcript tail was reported
    assert payload["session_id"] == "bc_1"


def test_reply_without_a_cursor_reports_a_pending_tail(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "assistant", "content": "an older answer"},
                               {"id": 2, "role": "user", "content": "ping"}]]

    code = _reply(monkeypatch, replies_peer, json_mode=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["status"] == "pending"
    assert payload["reply"] == ""


def test_reply_without_a_cursor_never_claims_not_delivered(monkeypatch, capsys, replies_peer):
    """Without a cursor there is no baseline that could call a message missing."""
    _RepliesPeer.snapshots = [[]]

    code = _reply(monkeypatch, replies_peer, json_mode=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["status"] == "pending"


def test_reply_with_a_cursor_hides_an_older_answer(monkeypatch, capsys, replies_peer):
    """The cursor is what makes "is this reply to MY message?" answerable: an answer that predates
    it must not be handed back as if it were ours."""
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "old"},
                               {"id": 2, "role": "assistant", "content": "old answer"},
                               {"id": 3, "role": "user", "content": "ping"}]]

    code = _reply(monkeypatch, replies_peer, after=2, json_mode=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["status"] == "pending"
    assert payload["rows_seen"] == 1
    assert payload["reply"] == ""


def test_reply_after_the_cursor_is_completed(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "old"},
                               {"id": 2, "role": "assistant", "content": "old answer"},
                               {"id": 3, "role": "user", "content": "ping"},
                               {"id": 4, "role": "assistant", "content": "pong"}]]

    code = _reply(monkeypatch, replies_peer, after=2)
    out = capsys.readouterr().out

    assert code == 0
    assert out == "pong\n"


def test_reply_after_a_cursor_with_nothing_after_it_is_not_delivered(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "old"},
                               {"id": 2, "role": "assistant", "content": "old answer"}]]

    code = _reply(monkeypatch, replies_peer, after=2, json_mode=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload["status"] == "not_delivered"
    assert payload["reply"] == ""


def test_reply_keeps_a_bare_silence_marker_from_the_sender(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "ping"},
                               {"id": 2, "role": "assistant", "content": "NO_REPLY"}]]

    code = _reply(monkeypatch, replies_peer)
    out = capsys.readouterr().out

    assert code == 0
    assert out.strip() == "(no reply)"
    assert "NO_REPLY" not in out


def test_reply_ends_with_the_last_assistant_row_with_non_empty_content(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "ping"},
                               {"id": 2, "role": "assistant", "content": "first"},
                               {"id": 3, "role": "assistant", "content": "second"}]]

    code = _reply(monkeypatch, replies_peer, json_mode=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["reply"] == "second"


def test_reply_never_creates_a_session(monkeypatch, capsys, replies_peer):
    """A READ verb must not write: with no canonical Bot Chat on the peer, `reply` says so and
    stops — it does not create one (that is `dm`/`run`'s job)."""
    _RepliesPeer.sessions = []

    code = _reply(monkeypatch, replies_peer)
    err = capsys.readouterr().err

    assert code == 1
    assert "no 'Bot Chat' session" in err
    assert _RepliesPeer.posts == []


def test_reply_performs_no_post_at_all(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "ping"},
                               {"id": 2, "role": "assistant", "content": "pong"}]]

    code = _reply(monkeypatch, replies_peer)
    capsys.readouterr()

    assert code == 0
    assert _RepliesPeer.posts == []


def test_reply_honours_an_explicit_session(monkeypatch, capsys, replies_peer):
    _RepliesPeer.sessions = []      # no lookup may be needed: the session was named
    _RepliesPeer.snapshots = [[{"id": 1, "role": "assistant", "content": "pong"}]]

    code = _reply(monkeypatch, replies_peer, session="bc_named")
    out = capsys.readouterr().out

    assert code == 0
    assert out == "pong\n"
    assert not any(path.startswith("/api/sessions?") for path in _RepliesPeer.gets)


def test_reply_reports_an_unknown_session_with_the_peers_detail(monkeypatch, capsys, replies_peer):
    _RepliesPeer.messages_status = 404

    code = _reply(monkeypatch, replies_peer, session="gone")
    err = capsys.readouterr().err

    assert code == 1
    assert "gone" in err
    assert "Session not found: gone" in err


def test_reply_wait_polls_until_the_turn_answers(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "ping"}],
                              [{"id": 1, "role": "user", "content": "ping"},
                               {"id": 2, "role": "assistant", "content": "pong"}]]
    monkeypatch.setattr(peer_cmd, "REPLY_POLL_S", 0.05)

    code = _reply(monkeypatch, replies_peer, after=0, wait=5)
    out = capsys.readouterr().out

    assert code == 0
    assert out == "pong\n"
    assert _RepliesPeer.reads >= 2


def test_reply_wait_gives_up_at_the_budget_as_pending(monkeypatch, capsys, replies_peer):
    _RepliesPeer.snapshots = [[{"id": 1, "role": "user", "content": "ping"}]]
    monkeypatch.setattr(peer_cmd, "REPLY_POLL_S", 0.05)

    code = _reply(monkeypatch, replies_peer, after=0, wait=1, json_mode=True)
    payload = json.loads(capsys.readouterr().out)

    assert code == 3
    assert payload["status"] == "pending"
    assert payload["rows_seen"] == 1


@pytest.mark.parametrize("wait", [-1, "-2"])
def test_reply_rejects_a_negative_wait(monkeypatch, capsys, replies_peer, wait):
    code = _reply(monkeypatch, replies_peer, wait=wait)

    assert code == 2
    assert "--wait" in capsys.readouterr().err
    assert _RepliesPeer.gets == []              # a usage error never touches the wire


def test_reply_rejects_a_non_integer_cursor(monkeypatch, capsys, replies_peer):
    code = _reply(monkeypatch, replies_peer, after="row-two")

    assert code == 2
    assert "--after" in capsys.readouterr().err
    assert _RepliesPeer.gets == []
