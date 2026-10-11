"""A denied model cannot be RESOLVED for a run (card t_a1ae7cc6, acceptance case A4).

The point of guarding the shared resolution path is that the refusal happens before any credential
or endpoint work: a run that is going to be refused must not first make a provider call, and it must
not quietly land on a fallback model, because a silent fallback still spends.
"""

from __future__ import annotations

import pytest

from hermes_cli.model_denylist import ModelDeniedError
from hermes_cli.runtime_provider import resolve_runtime_provider

BANNED = "deepseek-v4-pro"


def test_a_denied_target_model_is_refused_with_no_fallback():
    with pytest.raises(ModelDeniedError) as excinfo:
        resolve_runtime_provider(requested="deepseek", target_model=BANNED)
    assert "denied_model:deepseek-v4-pro" in str(excinfo.value)
    assert excinfo.value.denial.source == "build_floor"


def test_the_refusal_fires_before_any_credential_resolution():
    """No api key, no base_url, no requested provider - it still refuses on the DENIAL.

    If the guard ran late, this call would fail on a credential/auth error instead, which is the
    difference between refusing a banned model and merely failing to configure it.
    """
    with pytest.raises(ModelDeniedError):
        resolve_runtime_provider(target_model="deepseek/DeepSeek-V4-Pro-0813")


def test_a_permitted_target_model_is_not_refused_by_the_denylist():
    """A permitted model must fall through to the ordinary resolution ladder.

    Any error here must therefore be a resolution/auth error, never ModelDeniedError.
    """
    with pytest.raises(Exception) as excinfo:
        resolve_runtime_provider(requested="definitely-not-a-provider", target_model="deepseek-flash")
    assert not isinstance(excinfo.value, ModelDeniedError)
