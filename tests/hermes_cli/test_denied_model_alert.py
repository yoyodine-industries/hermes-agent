"""The refusal ACTS: exactly one card per (lane, model, day) (card t_a1ae7cc6, case A6).

A refusal that only raises is a silent failure by the estate's own rule - the spend stops, but
nobody is told why and the misconfiguration fires again tomorrow. So the refusal files one acting
card, routed deterministically, and is idempotent within the day.

The shape mirrors the deployed one-card-per-lane filer's own test
(``tests/test_model_egress_sentinel.py::test_alert_is_idempotent_within_the_day``), because this
module reuses that filer's instrument and key contract rather than inventing a second one.
"""

from __future__ import annotations

from hermes_cli.model_denial_alert import (
    board_for,
    current_lane,
    file_denial_card,
    filing_key,
    route,
)
from hermes_cli.model_denylist import denial

BANNED = "deepseek-v4-pro"


def _denial(model: str = BANNED):
    found = denial(model)
    assert found is not None
    return found


# --------------------------------------------------------------------------- #
# the filing contract
# --------------------------------------------------------------------------- #

def test_the_first_refusal_files_exactly_one_card(fake_denial_board):
    tid = file_denial_card(_denial(), lane="platform-coder", cause="pin")
    assert tid == "t_synthetic"
    assert len(fake_denial_board.calls) == 1
    call = fake_denial_board.calls[0]
    assert call["key"].startswith("model-denied:platform-coder:deepseek-v4-pro:")
    assert "denied_model:deepseek-v4-pro" in call["body"]


def test_a_second_refusal_the_same_day_files_none(fake_denial_board):
    """The idempotency the ruling asks for: same lane, same model, same day -> one card."""
    file_denial_card(_denial(), lane="laneA", cause="pin")
    file_denial_card(_denial(), lane="laneA", cause="pin")
    assert len(fake_denial_board.calls) == 1, "the same lane+model+day must file exactly one card"


def test_a_different_lane_files_its_own_card(fake_denial_board):
    file_denial_card(_denial(), lane="laneA", cause="pin")
    file_denial_card(_denial(), lane="laneB", cause="pin")
    assert len(fake_denial_board.calls) == 2
    assert {c["key"].split(":")[1] for c in fake_denial_board.calls} == {"laneA", "laneB"}


def test_a_different_day_is_a_new_card(fake_denial_board):
    """Filing is per DAY: yesterday's card does not silence today's refusal."""
    import time

    file_denial_card(_denial(), lane="laneA", cause="pin", now=time.time() - 86400 * 2)
    file_denial_card(_denial(), lane="laneA", cause="pin", now=time.time())
    assert len(fake_denial_board.calls) == 2


# --------------------------------------------------------------------------- #
# routing is deterministic (ruling B3)
# --------------------------------------------------------------------------- #

def test_a_config_or_pin_cause_routes_to_the_lane_design_authority():
    assert route("platform-coder", "config") == "platform-stl"
    assert route("research-stl", "pin") == "research-stl"
    assert route("financially-coder", "cron") == "financially-stl"


def test_a_code_or_curation_cause_routes_to_the_coder_seat():
    assert route("platform-stl", "code") == "platform-coder"
    assert route("research-coder", "catalog") == "platform-coder"


def test_each_lane_family_files_on_its_own_board():
    assert board_for("platform-coder") == "platform"
    assert board_for("financially-stl") == "financially"
    assert board_for("research-sme") == "research"
    assert board_for("default") == "default"
    assert board_for("unknown-lane") == "platform"


# --------------------------------------------------------------------------- #
# the key + the lane
# --------------------------------------------------------------------------- #

def test_the_key_normalizes_the_model_id():
    """An alias must key to the SAME card as the canonical id - one model, one card."""
    a = filing_key("laneA", "deepseek-v4-pro", now=1_700_000_000)
    b = filing_key("laneA", "deepseek/DeepSeek-V4-Pro-0813", now=1_700_000_000)
    assert a == b
    assert a == "model-denied:laneA:deepseek-v4-pro:20231114"


def test_the_lane_is_read_from_the_profile_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes" / "profiles" / "platform-coder"))
    assert current_lane() == "platform-coder"


def test_a_fleet_root_home_is_the_host_seat(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    assert current_lane() == "default"


def test_filing_never_raises_when_the_board_is_unreachable(monkeypatch):
    """Filing is best-effort: a broken board must never soften the refusal."""
    import sys

    class _Broken:
        @staticmethod
        def create_task(*a, **k):  # pragma: no cover - it must never be reached
            raise AssertionError("should not be reached")

    class _KBC:
        @staticmethod
        def connect(*a, **k):
            raise RuntimeError("no board today")

    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", _Broken)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db_connect", _KBC)
    assert file_denial_card(_denial(), lane="laneA", cause="pin") is None
