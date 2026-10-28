"""What ``hermes peer dm`` tells the sender when the turn does not come back.

A peer DM is one synchronous ``POST /api/sessions/{id}/chat`` and the receiving gateway runs the
turn to completion whatever the client does. So a timeout while awaiting the response means the
message IS in the peer's Bot Chat and is being answered — the opposite of unreachable. Reporting
it unreachable makes the sending bot resend, and the teammate runs the same turn twice. A timeout
while connecting means the request never arrived, and must stay unreachable.

A response-phase timeout is exit **3** — "accepted, reply not collected" — deliberately distinct
from 1 ("nothing on the wire") so a caller can tell delivered from not-delivered mechanically. It
also prints the transcript row the message landed in (when the confirm-read can pin it) and the
``hermes peer reply`` command that collects what the peer answers, so the reply is no longer
uncollectable by the sender.

The client's own bound is the CALL CEILING (``_dm_ceiling``), not the ``--wait`` budget asked of
the peer: the delivery POST, any replay and the confirm-read together never run past it, which is
what keeps a caller whose own bound is shorter (an agent terminal cap, a cron shim, ``timeout N``)
from being killed before the verdict is printed.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import urllib.error
from types import SimpleNamespace

import pytest

from hermes_cli.subcommands import peer as peer_mod

SESSION = "20260916_bot_chat"


def _run(monkeypatch, capsys, *, base="http://192.168.2.55:8642", raise_on_post=None, raise_on_session=None):
    def _ensure(base, key):
        if raise_on_session is not None:
            raise raise_on_session
        return SESSION

    monkeypatch.setattr(peer_mod, "_ensure_bot_chat", _ensure)
    # The confirm-read's PACING is not what these cases assert (only the verdict and the exit code
    # are); zero it so the accepted path does not sleep a second per attempt in every case here.
    monkeypatch.setattr(peer_mod, "CONFIRM_READ_INTERVAL_S", 0)
    if raise_on_post is not None:
        def _request(url, key, **kwargs):
            raise raise_on_post

        monkeypatch.setattr(peer_mod, "_request", _request)
    code = peer_mod._peer_dm(SimpleNamespace(json=False), "hello", "mini", None, base, "key")
    return code, capsys.readouterr().err


@pytest.mark.parametrize(
    ("raise_on_post", "raise_on_session", "expected", "forbidden", "expected_code"),
    [
        (TimeoutError("timed out"), None, "accepted the message but its turn is still running", "Could not reach", 3),
        # A timeout urllib WRAPS in URLError is a lost response on this line: `_is_read_timeout`
        # (kept whole) classifies both shapes as "the request was sent, the answer was lost", so
        # the idempotent replay runs — and when it is lost too, the outcome is the same verdict.
        (urllib.error.URLError(TimeoutError("timed out")), None, "accepted the message but its turn is still running", "Could not reach", 3),
        (urllib.error.URLError(ConnectionRefusedError(61, "Connection refused")), None, "Could not reach peer", "still running", 1),
        (OSError(51, "Network is unreachable"), None, "Could not reach peer", "still running", 1),
        (TimeoutError("timed out"), TimeoutError("timed out"), "Could not reach peer", "still running", 1),
        (urllib.error.HTTPError("http://peer/chat", 503, "Service Unavailable", {}, None), None,
         "rejected the request (HTTP 503)", "still running", 1),
    ],
    ids=["response-timeout", "wrapped-response-timeout", "connection-refused", "network-unreachable",
         "timeout-before-the-session-is-known", "http-rejection"],
)
def test_only_a_timeout_on_an_accepted_turn_is_reported_as_delivered(
    monkeypatch, capsys, raise_on_post, raise_on_session, expected, forbidden, expected_code
):
    """Two things must both hold to call the message delivered: the Bot Chat was already resolved
    (the peer answered a moment ago) and the timeout hit while awaiting the response (urllib raises
    that one bare). A connect-phase failure, a refusal, or a status code proves nothing arrived —
    those keep exit 1 "nothing on the wire". A response-phase timeout is exit 3 "accepted, reply not
    collected": the message WAS taken, so the caller is told to collect rather than resend."""
    code, err = _run(monkeypatch, capsys, raise_on_post=raise_on_post, raise_on_session=raise_on_session)

    assert code == expected_code
    assert expected in err
    assert forbidden not in err
    if "still running" in expected:
        assert SESSION in err
        assert "Do NOT resend" in err
        assert f"hermes peer reply mini --session {SESSION}" in err


@pytest.mark.parametrize("phase", ["response", "connect"])
def test_the_real_urllib_stack_raises_the_signatures_the_branch_relies_on(monkeypatch, capsys, phase):
    """Through the real opener, not a hand-built exception: urllib wraps a connect/send failure in
    ``URLError`` and lets a timeout on the response through bare. Getting that backwards reports a
    message that never arrived as delivered, and the sender is told not to resend it.

    Either way the client must NOT sit past its own ceiling: this call's ceiling is tiny, so the
    idempotent replay cannot fit inside it and the verdict is printed instead."""
    for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    # The whole client wait, the delivery floor and the confirm-read are all made tiny: this test is
    # about which EXCEPTION SHAPE the real stack raises, not about how long anything waits.
    monkeypatch.setattr(peer_mod, "DM_DEFAULT_TIMEOUT_S", 0.5)
    monkeypatch.setattr(peer_mod, "MIN_DELIVERY_S", 0.5)
    monkeypatch.setattr(peer_mod, "CONFIRM_READ_TIMEOUT_S", 0.2)
    monkeypatch.setattr(peer_mod, "CONFIRM_READ_INTERVAL_S", 0)

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    held = []
    done = threading.Event()

    def _accept_and_never_answer():
        with contextlib.suppress(OSError):  # the connect phase never dials, so close() ends accept()
            conn, _ = server.accept()
            held.append(conn)
            conn.recv(65536)
            done.wait(10)

    thread = threading.Thread(target=_accept_and_never_answer, daemon=True)
    thread.start()
    if phase == "connect":
        def _connect_times_out(*args, **kwargs):
            raise TimeoutError("timed out")

        monkeypatch.setattr(socket, "create_connection", _connect_times_out)
    try:
        code, err = _run(monkeypatch, capsys, base=f"http://127.0.0.1:{server.getsockname()[1]}")
    finally:
        done.set()
        server.close()
        for conn in held:
            conn.close()

    # The request WAS sent and the peer took the turn: exit 3, collect it later.
    assert code == 3
    assert "accepted the message but its turn is still running" in err and "Do NOT resend" in err
    assert "Could not reach peer" not in err


# ── the wait is the CALLER'S bound, and a reply that misses it leaves a handle ─


def _dm_with_timeout(monkeypatch, capsys, *, args_extra=None, post_exc=None, scripts=None,
                     message="hello", json_mode=False):
    """Drive one `dm` whose POST fails and whose transcript reads follow ``scripts``.

    ``scripts`` is a list of transcript snapshots; the last one repeats. Returns
    ``(exit_code, stdout, stderr, seen)`` where ``seen`` records the FIRST delivery POST's socket
    timeout, how many POSTs were made, and how many transcript reads the client made.
    """
    seen: dict = {"reads": 0, "posts": 0}
    reads = [list(snapshot) for snapshot in (scripts or [])]

    def _request(url, key, **kwargs):
        if url.endswith("/chat"):
            seen.setdefault("timeout", kwargs.get("timeout"))
            seen["posts"] += 1
            raise (post_exc or TimeoutError("timed out"))
        if "/messages" in url:
            index = min(seen["reads"], len(reads) - 1) if reads else None
            seen["reads"] += 1
            data = reads[index] if index is not None else []
            return {"object": "list", "session_id": SESSION, "data": [dict(row) for row in data]}
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(peer_mod, "_ensure_bot_chat", lambda base, key: SESSION)
    monkeypatch.setattr(peer_mod, "_request", _request)
    monkeypatch.setattr(peer_mod, "CONFIRM_READ_INTERVAL_S", 0)
    attrs = {"json": json_mode, "target": "mini", **(args_extra or {})}
    code = peer_mod._peer_dm(SimpleNamespace(**attrs), message, "mini", None, "http://peer", "key")
    captured = capsys.readouterr()
    return code, captured.out, captured.err, seen


def test_dm_default_wait_is_the_short_caller_bound_not_a_six_hundred_second_one(monkeypatch, capsys):
    """The regression this card exists for: the old hard-coded 600 s wait outlived every ordinary
    caller bound, so the client was killed before it could print the verdict that says "delivered,
    do NOT resend" — and silence became the answer. Without `--timeout` the client now waits
    ``DM_DEFAULT_TIMEOUT_S`` and then PRINTS the verdict (exit 3) instead of nothing."""
    code, out, err, seen = _dm_with_timeout(monkeypatch, capsys)

    assert seen["timeout"] == peer_mod.DM_DEFAULT_TIMEOUT_S == 60
    assert code == 3
    assert out == ""
    assert "accepted the message but its turn is still running" in err
    assert "600" not in err


def test_dm_timeout_is_what_the_caller_asked_for(monkeypatch, capsys):
    code, _out, err, seen = _dm_with_timeout(monkeypatch, capsys, args_extra={"timeout": 10})

    assert seen["timeout"] == 10
    assert code == 3
    assert "still running after 0s" in err      # the verdict reports the ACTUAL wait elapsed


def test_dm_timeout_is_floored_so_the_request_actually_leaves(monkeypatch, capsys):
    """A caller bound below ``MIN_DELIVERY_S`` would usually expire before the POST even goes out
    (`hermes <subcommand>` costs ~10 s of launcher/bootstrap), which would report "not delivered"
    for a message about to be sent. The floor keeps the POST a real chance to leave."""
    _code, _out, _err, seen = _dm_with_timeout(monkeypatch, capsys, args_extra={"timeout": 1})

    assert seen["timeout"] == peer_mod.MIN_DELIVERY_S


# ── the ceiling arithmetic (E1) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({}, 60.0),                        # no flags -> DM_DEFAULT_TIMEOUT_S
        ({"wait_seconds": 30.0}, 60.0),    # --wait 30 -> 30 + the receipt-window slack
        ({"wait_seconds": 600.0}, 630.0),  # --wait 600 still buys the long inline wait
        ({"timeout": 20}, 20.0),           # --timeout sets the ceiling exactly
        ({"timeout": 2}, 5.0),             # ... floored at MIN_DELIVERY_S
        ({"no_wait": True}, 3.0),          # --no-wait never waits for the reply
    ],
)
def test_the_call_ceiling_is_the_clients_own_bound(flags, expected):
    assert peer_mod._dm_ceiling(SimpleNamespace(**flags)) == expected


def test_dm_no_wait_uses_only_the_short_grace_and_still_hands_back_the_handle(monkeypatch, capsys):
    """`--no-wait` never waits for the reply past ``NO_WAIT_GRACE_S``; a turn that does not answer
    inside that grace still yields the exit-3 verdict WITH the collect command, not silence."""
    code, out, err, seen = _dm_with_timeout(monkeypatch, capsys, args_extra={"no_wait": True})

    assert seen["timeout"] == peer_mod.NO_WAIT_GRACE_S
    assert code == 3
    assert out == ""
    assert "Do NOT resend" in err
    assert f"hermes peer reply mini --session {SESSION}" in err


def test_dm_no_wait_returns_a_reply_that_beats_the_grace(monkeypatch, capsys):
    """`--no-wait` is not "ignore the reply": when it is already there, it is printed and exit 0."""
    monkeypatch.setattr(peer_mod, "_ensure_bot_chat", lambda base, key: SESSION)
    monkeypatch.setattr(peer_mod, "_request", lambda url, key, **kwargs: {
        "object": "hermes.session.chat.completion", "session_id": SESSION,
        "message": {"role": "assistant", "content": "fast reply"}})

    code = peer_mod._peer_dm(SimpleNamespace(json=False, no_wait=True), "hello", "mini", None,
                             "http://peer", "key")

    assert code == 0
    assert "fast reply" in capsys.readouterr().out


# ── the ceiling is not overrun by the replay (E1, last clause) ───────────────


def test_the_replay_is_skipped_when_it_cannot_fit_the_ceiling(monkeypatch, capsys):
    """A ceiling that lets the client run 30 s past itself is the defect again.

    The idempotent replay needs its whole ``_DM_REPLAY_TIMEOUT_S`` inside what remains of the
    ceiling. A caller-bound ``--timeout 10`` has spent it by the time the read timeout fires, so the
    client reports the outcome instead of spending 30 s nobody budgeted for — and, crucially, it
    does NOT re-POST: one turn, one delivery."""
    code, _out, _err, seen = _dm_with_timeout(monkeypatch, capsys, args_extra={"timeout": 10})

    assert code == 3
    assert seen["posts"] == 1


def test_the_replay_still_runs_when_the_ceiling_has_room(monkeypatch, capsys):
    """The D3 replay is kept whole: with the default ceiling there is room for it, so a lost
    response is re-issued once against the same idempotency key."""
    code, _out, err, seen = _dm_with_timeout(monkeypatch, capsys)

    assert code == 3
    assert seen["posts"] == 2
    assert "still running" in err


def test_dm_into_an_open_bot_chat_prints_the_collect_command(monkeypatch, capsys):
    """The live-Bot-Chat handoff is unchanged except that its message now carries the collect
    command, so the reply of a turn taken over by an open Desktop chat is collectable too."""
    monkeypatch.setattr(peer_mod, "_ensure_bot_chat", lambda base, key: SESSION)
    monkeypatch.setattr(peer_mod, "_request", lambda url, key, **kwargs: {
        "object": "hermes.session.chat.queued", "session_id": SESSION, "status": "queued"})

    code = peer_mod._peer_dm(SimpleNamespace(json=False, target="mini"), "hello", "mini", None,
                             "http://peer", "key")
    out = capsys.readouterr().out

    assert code == 0
    assert "Do NOT resend" in out
    assert f"hermes peer reply mini --session {SESSION}" in out


# ── the confirm-read: pinning OUR row so `reply --after` cannot point at an older turn ─


_OLDER = [{"id": 1, "role": "user", "content": "an older question"},
          {"id": 2, "role": "assistant", "content": "an older answer"}]


def test_dm_pins_the_row_whose_content_matches(monkeypatch, capsys):
    code, _out, err, seen = _dm_with_timeout(
        monkeypatch, capsys,
        scripts=[_OLDER, _OLDER + [{"id": 3, "role": "user", "content": "hello"}]])

    assert code == 3
    assert seen["reads"] >= 2          # the first read shows nothing of ours, the second does
    assert f"session {SESSION}, row 3" in err
    assert f"hermes peer reply mini --session {SESSION} --after 3" in err


def test_dm_json_reports_a_confirmed_delivery_with_the_pinned_row(monkeypatch, capsys):
    code, out, _err, _seen = _dm_with_timeout(
        monkeypatch, capsys, json_mode=True,
        scripts=[_OLDER, _OLDER + [{"id": 3, "role": "user", "content": "hello"}]])

    payload = json.loads(out)
    assert code == 3
    assert payload["status"] == "pending"
    assert payload["delivery"] == "confirmed"
    assert payload["after"] == 3
    assert payload["session_id"] == SESSION
    assert payload["peer"] == "mini"


def test_dm_does_not_pin_a_row_that_is_not_ours(monkeypatch, capsys):
    """The pin is a content match, and nothing else is good enough.

    This line does not read the transcript BEFORE the POST — that request would sit in front of
    every delivery on a route the receipt harness counts call-by-call — so a peer that rewrote our
    message cannot be called ours by elimination. The verdict then claims exactly what it knows:
    ``accepted``, no ``--after``, and the line that says how to check."""
    code, _out, err, _seen = _dm_with_timeout(
        monkeypatch, capsys,
        scripts=[_OLDER, _OLDER + [{"id": 3, "role": "user", "content": "hello (rewritten)"}]])

    assert code == 3
    assert "--after" not in err
    assert f"hermes peer reply mini --session {SESSION}\n" in err
    assert "Check that the newest user row" in err


def test_dm_claims_nothing_when_several_user_rows_appeared(monkeypatch, capsys):
    """More than one user row and none of them ours means we cannot say which is ours, so the
    verdict says ``accepted`` — "the request was sent" — and omits the ``--after`` handle."""
    code, out, _err, _seen = _dm_with_timeout(
        monkeypatch, capsys, json_mode=True,
        scripts=[_OLDER, _OLDER + [{"id": 3, "role": "user", "content": "someone else"},
                                   {"id": 4, "role": "user", "content": "and someone else"}]])

    payload = json.loads(out)
    assert code == 3
    assert payload["status"] == "pending"
    assert payload["delivery"] == "accepted"
    assert payload["after"] is None


def test_dm_unpinned_verdict_prints_the_cursorless_collect_line(monkeypatch, capsys):
    """The unpinned verdict is still actionable: it names the session, drops ``--after`` (so it
    cannot be pointed at another turn by accident) and says how to check the row is ours."""
    code, _out, err, _seen = _dm_with_timeout(
        monkeypatch, capsys,
        scripts=[_OLDER, _OLDER + [{"id": 3, "role": "user", "content": "someone else"},
                                   {"id": 4, "role": "user", "content": "and someone else"}]])

    assert code == 3
    assert "--after" not in err
    assert f"hermes peer reply mini --session {SESSION}\n" in err
    assert "Check that the newest user row" in err


def test_dm_claims_nothing_when_the_transcript_shows_no_new_row(monkeypatch, capsys):
    """The confirm-read is never fatal and never guesses: a transcript with no new user row
    downgrades the verdict's claim to ``accepted`` — it does not replace it with a traceback, and
    it never reports the peer unreachable."""
    code, _out, err, _seen = _dm_with_timeout(monkeypatch, capsys, scripts=[])

    assert code == 3
    assert "accepted the message but its turn is still running" in err
    assert "Do NOT resend" in err
    assert "Check that the newest user row" in err


def test_dm_rejects_timeout_and_no_wait_together(monkeypatch, capsys):
    monkeypatch.setattr(peer_mod, "_load_peers", lambda: {"mini": {"url": "http://x"}})
    monkeypatch.setattr(peer_mod, "_peer_secret", lambda name: "k" * 12)

    code = peer_mod.cmd_peer(SimpleNamespace(peer_action="dm", target="mini", message="hi",
                                             json=False, timeout=30, no_wait=True))

    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_dm_rejects_wait_and_no_wait_together(monkeypatch, capsys):
    """`--wait` is the budget asked OF the peer; `--no-wait` refuses to wait for its reply at all.
    Asking for both is a contradiction, not a preference."""
    monkeypatch.setattr(peer_mod, "_load_peers", lambda: {"mini": {"url": "http://x"}})
    monkeypatch.setattr(peer_mod, "_peer_secret", lambda name: "k" * 12)

    code = peer_mod.cmd_peer(SimpleNamespace(peer_action="dm", target="mini", message="hi",
                                             json=False, wait_seconds=30.0, no_wait=True))

    assert code == 2
    assert "mutually exclusive" in capsys.readouterr().err
