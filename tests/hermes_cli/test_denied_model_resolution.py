"""A denied model cannot be RESOLVED for a run (card t_a1ae7cc6, acceptance case A4).

The point of guarding the shared resolution path is that the refusal happens before any credential,
endpoint or transport work: a run that is going to be refused must not first make a provider call,
and it must not quietly land on a fallback model, because a silent fallback still spends.

The refusal also ACTS (ruling B3): it files exactly one idempotency-keyed card. These tests install
the ``fake_denial_board`` fixture so the assertion is over a recorded call and no real card is
written to the live board.
"""

from __future__ import annotations

import socket

import pytest

from hermes_cli.model_denylist import ModelDeniedError
from hermes_cli.runtime_provider import resolve_runtime_provider

BANNED = "deepseek-v4-pro"


def test_a_denied_target_model_is_refused_with_no_fallback(fake_denial_board):
    with pytest.raises(ModelDeniedError) as excinfo:
        resolve_runtime_provider(requested="deepseek", target_model=BANNED)
    assert "denied_model:deepseek-v4-pro" in str(excinfo.value)
    assert excinfo.value.denial.source == "build_floor"


def test_the_refusal_fires_before_any_credential_resolution(fake_denial_board):
    """No api key, no base_url, no requested provider - it still refuses on the DENIAL.

    If the guard ran late, this call would fail on a credential/auth error instead, which is the
    difference between refusing a banned model and merely failing to configure it.
    """
    with pytest.raises(ModelDeniedError):
        resolve_runtime_provider(target_model="deepseek/DeepSeek-V4-Pro-0813")


def test_a_permitted_target_model_is_not_refused_by_the_denylist(fake_denial_board):
    """A permitted model must fall through to the ordinary resolution ladder.

    Any error here must therefore be a resolution/auth error, never ModelDeniedError.
    """
    with pytest.raises(Exception) as excinfo:
        resolve_runtime_provider(requested="definitely-not-a-provider", target_model="deepseek-flash")
    assert not isinstance(excinfo.value, ModelDeniedError)


def test_a_denied_effective_model_makes_ZERO_transport_calls(fake_denial_board, monkeypatch):
    """A4, the part that matters: the refusal must not cost anything.

    A provider call is the spend, and a socket connect is the floor of one, so we count those and
    assert the count is zero - a ban that lets even one request out has already lost the argument
    it exists to win.
    """
    attempts: list = []
    real_connect = socket.socket.connect

    def _spy_connect(self, address):  # pragma: no cover - it must never run
        attempts.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", _spy_connect, raising=True)

    with pytest.raises(ModelDeniedError):
        resolve_runtime_provider(requested="deepseek", target_model=BANNED)

    assert attempts == [], "a denied model must not reach the network at all"


def test_the_refusal_files_exactly_one_card(fake_denial_board):
    """The refusal is not silent: it leaves one actionable card, addressed to the lane's STL."""
    with pytest.raises(ModelDeniedError):
        resolve_runtime_provider(requested="deepseek", target_model=BANNED)
    assert len(fake_denial_board.calls) == 1
    call = fake_denial_board.calls[0]
    assert call["key"].startswith("model-denied:")
    assert "denied_model:deepseek-v4-pro" in call["body"]
