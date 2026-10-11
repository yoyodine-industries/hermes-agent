"""A denied model cannot be named in a config - at load OR at `hermes config set` (card t_a1ae7cc6).

Acceptance cases A2 and A3. Both halves are tested here because they are different code paths and
easy to mistake for one: the load-time validator runs when a config is READ, while
`set_config_value` writes what it is told and never calls that validator - so a config can be
written through the CLI that would be refused on the next load. Testing only the validator would
have passed A2 while the file on disk said something else.
"""

from __future__ import annotations

import pytest

from hermes_cli.config import _refuse_denied_model_set, validate_config_structure

BANNED = "deepseek-v4-pro"


def _messages(config):
    return " | ".join(getattr(issue, "message", str(issue)) for issue in validate_config_structure(config))


def test_a_load_time_default_naming_a_denied_model_is_an_issue():
    assert "DENIED" in _messages({"model": {"default": BANNED}})


def test_a_load_time_alias_is_caught_too():
    assert "DENIED" in _messages({"model": {"default": "deepseek/DeepSeek-V4-Pro-0813"}})


@pytest.mark.parametrize("config", [
    {"auxiliary": {"summarizer": {"model": BANNED}}},
    {"cron": {"model": BANNED}},
    {"providers": {"deepseek": {"models": {BANNED: {"max_tokens": 1}}}}},
    {"moa": {"presets": {"default": {"reference_models": [{"provider": "openrouter", "model": BANNED}]}}}},
    {"moa": {"presets": {"default": {"aggregator": {"provider": "openrouter", "model": BANNED}}}}},
])
def test_every_config_location_that_can_name_a_model_is_checked(config):
    assert "DENIED" in _messages(config), config


def test_a_healthy_config_is_not_flagged():
    assert "DENIED" not in _messages({"model": {"default": "deepseek-flash"}})
    assert "DENIED" not in _messages({})


def test_set_refuses_a_denied_model_as_the_VALUE():
    with pytest.raises(SystemExit):
        _refuse_denied_model_set("model.default", BANNED)
    with pytest.raises(SystemExit):
        _refuse_denied_model_set("auxiliary.summarizer.model", BANNED)


def test_set_refuses_a_denied_model_that_is_named_IN_THE_KEY():
    with pytest.raises(SystemExit):
        _refuse_denied_model_set(f"providers.deepseek.models.{BANNED}", {"max_tokens": 1})


def test_set_allows_a_permitted_model_and_the_add_only_layer():
    assert _refuse_denied_model_set("model.default", "deepseek-flash") is None
    # Adding to the denylist IS the config layer working; refusing it would break the ban.
    assert _refuse_denied_model_set("model.denied_models", ["some-expensive-model"]) is None
    assert _refuse_denied_model_set("model.provider", "deepseek") is None
