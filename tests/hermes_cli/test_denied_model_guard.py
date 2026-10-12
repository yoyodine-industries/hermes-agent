"""The denied-model guard, and the deepseek profile, refuse a banned model (card t_a1ae7cc6).

The registry is the ONE place surfaces ask, so these tests ask IT rather than inspecting the
registry's source: if a surface gets no `denied_model` warning for a banned model, the ban is not
reachable from that surface no matter what the module says.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from hermes_cli.model_selection_guards import selection_warnings

REPO = Path(__file__).resolve().parents[2]


def _denied_warnings(model):
    return [w for w in selection_warnings(model) if w.kind == "denied_model"]


def test_the_registry_refuses_a_denied_model_from_one_place():
    denied = _denied_warnings("deepseek-v4-pro")
    assert len(denied) == 1, denied
    warning = denied[0]
    assert warning.model == "deepseek-v4-pro"
    assert "HARD REFUSAL" in warning.message
    assert "denied_model:deepseek-v4-pro" in warning.message


def test_the_registry_is_silent_for_a_permitted_model():
    assert _denied_warnings("deepseek-flash") == []
    assert _denied_warnings("Qwen3.6-35B") == []


def test_an_alias_is_refused_by_the_registry_too():
    assert len(_denied_warnings("deepseek/DeepSeek-V4-Pro-0813")) == 1


def test_the_deepseek_profile_no_longer_offers_the_banned_model():
    """Load the profile module and read its OWN list - not the source text."""
    path = REPO / "plugins" / "model-providers" / "deepseek" / "__init__.py"
    spec = importlib.util.spec_from_file_location("deepseek_profile_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "deepseek-v4-pro" not in module.deepseek.fallback_models, module.deepseek.fallback_models
    assert "deepseek-flash" in module.deepseek.fallback_models
