"""``hermes gateway admission retire``: the supported door for a fossil admission.

The verb itself resolves through the live owner (``prompt.resolve_unknown``), so these tests pin the
parts that must not silently rot: the parser wiring, the REQUIRED reason, and the refusal copy that
names the field and the lease.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_admission
from hermes_cli.subcommands.gateway import build_gateway_parser


def _h_gateway(args):  # pragma: no cover - identity only
    return "gateway"


def _h_proxy(args):  # pragma: no cover - identity only
    return "proxy"


def _h_gateway_enroll(args):  # pragma: no cover - identity only
    return "gateway_enroll"


def _gateway_parser():
    p = argparse.ArgumentParser(prog="hermes")
    sub = p.add_subparsers(dest="command")
    build_gateway_parser(sub, cmd_gateway=_h_gateway, cmd_proxy=_h_proxy,
                         cmd_gateway_enroll=_h_gateway_enroll)
    return p


def test_admission_retire_parser_requires_id_and_reason():
    ns = _gateway_parser().parse_args(
        ["gateway", "admission", "retire", "--id", "abc123", "--reason", "turn died in a crash"])
    assert ns.gateway_command == "admission"
    assert ns.admission_command == "retire"
    assert ns.admission_id == "abc123"
    assert ns.reason == "turn died in a crash"
    assert ns.func is _h_gateway


def test_retire_refuses_without_a_reason_before_touching_the_store(monkeypatch):
    opened = []
    monkeypatch.setattr(gateway_admission, "_admission", lambda admission_id: opened.append(admission_id))
    args = SimpleNamespace(admission_command="retire", admission_id="abc123", reason="   ", yes=True)
    assert gateway_admission.cmd_admission(args) == 1
    assert opened == [], "a missing reason must refuse before any read or owner round-trip"


def test_refusal_text_names_the_field_and_the_lease():
    class Refusal(Exception):
        data = {"reason": "admission_running", "field": "lease",
                "detail": "live turn lease held by pid 4242, 279s left"}

    text = gateway_admission._refusal_text(Refusal("admission_running"))
    assert "admission_running" in text and "lease" in text and "pid 4242" in text


FOSSIL = {"admission_id": "abc123", "target_session_id": "local-abc", "status": "started",
          "outcome": None, "owner_epoch": 3, "generation": 7}
SNAPSHOT = {"session_id": "local-abc", "stored_session_id": "local-abc",
            "pending": [{"admission_id": "abc123", "status": "started", "execution_generation": 7}]}


def _owner(monkeypatch, reply):
    """A stub owner on a real websocket: records every call, answers ``reply(method)``."""
    from websockets.asyncio.server import serve

    from hermes_cli.gateway import gateway_command

    calls = []

    async def peer(ws):
        async for raw in ws:
            request = json.loads(raw)
            calls.append((request["method"], request["params"]))
            answer = reply(request["method"])
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], **answer}))

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_gateway_parser(sub, cmd_gateway=gateway_command, cmd_proxy=lambda a: None,
                         cmd_gateway_enroll=lambda a: None)
    return serve, peer, calls, parser


@pytest.mark.asyncio
async def test_retire_resolves_through_the_owner_and_carries_the_reason(monkeypatch, capsys):
    serve, peer, calls, parser = _owner(monkeypatch, lambda method: {
        "result": SNAPSHOT if method == "session.resume" else {**FOSSIL, "status": "terminal",
                                                              "outcome": "interrupted"}})
    monkeypatch.setattr(gateway_admission, "_admission", lambda aid: dict(FOSSIL))
    async with serve(peer, "127.0.0.1", 0) as server:
        monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        args = parser.parse_args(["gateway", "admission", "retire", "--id", "abc123",
                                  "--reason", "turn died in a crash", "--yes"])
        assert await asyncio.to_thread(args.func, args) is None  # exit 0

    assert calls == [
        ("session.resume", {"session_id": "local-abc"}),
        ("prompt.resolve_unknown", {"session_id": "local-abc", "admission_id": "abc123",
                                    "execution_generation": 7, "reason": "turn died in a crash"}),
    ]
    out = capsys.readouterr().out
    assert "Retired abc123" in out and "outcome=interrupted" in out


@pytest.mark.asyncio
async def test_retire_refusal_names_the_live_lease_and_exits_nonzero(monkeypatch, capsys):
    def reply(method):
        if method == "session.resume":
            return {"result": SNAPSHOT}
        return {"error": {"code": 4001, "message": "admission_running", "data": {
            "reason": "admission_running", "field": "lease",
            "detail": "live turn lease held by pid 4242, 279s left"}}}

    serve, peer, calls, parser = _owner(monkeypatch, reply)
    monkeypatch.setattr(gateway_admission, "_admission", lambda aid: dict(FOSSIL))
    async with serve(peer, "127.0.0.1", 0) as server:
        monkeypatch.setenv("HERMES_TUI_GATEWAY_URL", f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        args = parser.parse_args(["gateway", "admission", "retire", "--id", "abc123",
                                  "--reason", "cleanup", "--yes"])
        with pytest.raises(SystemExit) as exit_code:
            await asyncio.to_thread(args.func, args)
    assert exit_code.value.code == 1
    err = capsys.readouterr().err
    assert "admission_running" in err and "pid 4242" in err
