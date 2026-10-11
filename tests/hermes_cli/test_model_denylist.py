"""The denylist predicate: one place, and the properties the ban depends on (card t_a1ae7cc6).

These tests exist because a ban that is easy to spell around is not a ban. The interesting cases
are the ALIASES (case, vendor prefix, dots, date suffix), the ADD-ONLY property of the config
layer, and the fact that the refusal is a hard error rather than a warning a caller can wave past.
"""

from __future__ import annotations

import pytest

from hermes_cli.model_denylist import (
    DENIED_MODELS,
    ModelDeniedError,
    assert_allowed,
    canonical,
    denial,
    effective_denied,
    is_denied,
)

BANNED = "deepseek-v4-pro"


@pytest.mark.parametrize("spelling", [
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-pro-0813",
    "DeepSeek-V4-Pro",
    "DEEPSEEK-V4-PRO-0813",
    "deepseek/DeepSeek-V4-Pro-0813",
    "deepseek.v4.pro",
    "  deepseek-v4-pro  ",
])
def test_every_alias_of_the_banned_model_is_denied(spelling):
    """An alias must not be a way around the floor."""
    found = denial(spelling)
    assert found is not None, spelling
    assert found.source == "build_floor", (spelling, found)
    assert found.canonical_id == BANNED, (spelling, found.canonical_id)


@pytest.mark.parametrize("model", [
    "deepseek-flash",
    "deepseek-v3",
    "deepseek-v4",            # the FAMILY is not banned; the PRO model is
    "deepseek/deepseek-v4",
    "Qwen3.6-35B",
    "",
    None,
])
def test_a_model_that_is_not_banned_is_not_denied(model):
    assert denial(model) is None, model
    assert is_denied(model) is False, model


def test_the_config_layer_ADDS_to_the_floor():
    config = {"model": {"denied_models": ["some-expensive-model", "another/One-0813"]}}
    found = denial("some-expensive-model", config=config)
    assert found is not None and found.source == "config"
    assert denial("another/one", config=config) is not None, "the alias must match here too"
    assert denial(BANNED, config=config).source == "build_floor"


def test_the_config_layer_can_NEVER_subtract_from_the_floor():
    """A config that omits - or even empties - the key still bans. That is the whole point."""
    for config in ({"model": {}}, {"model": {"denied_models": []}}, {}, {"model": None}):
        found = denial(BANNED, config=config)
        assert found is not None, config
        assert found.source == "build_floor", (config, found)


def test_an_unreadable_config_neither_crashes_nor_unbans():
    class Explodes(dict):
        def get(self, *_a, **_k):
            raise RuntimeError("config is unreadable")

    assert denial(BANNED, config=Explodes()) is not None
    assert denial(BANNED, config={"model": "not-a-dict"}) is not None
    assert denial(BANNED, config={"model": {"denied_models": "not-a-list"}}) is not None


def test_effective_denied_is_the_floor_union_the_config():
    base = effective_denied(config={"model": {"denied_models": []}})
    assert BANNED in base
    with_extra = effective_denied(config={"model": {"denied_models": ["extra-model"]}})
    assert "extra-model" in with_extra and BANNED in with_extra
    assert DENIED_MODELS, "the floor must not be empty"


def test_the_refusal_is_HARD_and_names_no_fallback():
    """`ModelDeniedError`, with a machine-readable reason - never a warning to confirm past."""
    with pytest.raises(ModelDeniedError) as excinfo:
        assert_allowed(BANNED, where="cron run_job")
    error = excinfo.value
    assert error.denial.machine_reason == "denied_model:deepseek-v4-pro"
    assert "denied_model:deepseek-v4-pro" in str(error)
    assert "cron run_job" in str(error), "the refusal must say WHERE it refused"
    assert "no fallback model may be substituted" in str(error)


def test_assert_allowed_passes_a_permitted_model():
    assert assert_allowed("deepseek-flash") is None


def test_canonical_folds_the_three_alias_axes():
    assert canonical("deepseek/DeepSeek-V4-Pro-0813") == BANNED
    assert canonical("deepseek.v4.pro") == BANNED
    assert canonical("") == ""
    assert canonical(None) == ""
