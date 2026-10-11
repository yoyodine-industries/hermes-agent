"""A card or a surface may not PIN a denied model (card t_a1ae7cc6, acceptance case A5).

``tasks.model_override`` / ``tasks.provider_override`` are how the kanban dispatcher pins the model
a spawned worker runs, and how the CLI/dashboard sets that pin. Both go through
``kanban_db._validate_model_override``, so that is the one place the ban has to hold - and these
tests drive the REAL ``create_task`` / ``set_model_override``, not just the helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.model_denylist import ModelDeniedError

BANNED = "deepseek-v4-pro"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_a_card_may_not_pin_a_denied_model_in_its_model_override(kanban_home, fake_denial_board):
    with kbc.connect() as conn:
        with pytest.raises(ModelDeniedError) as excinfo:
            kb.create_task(conn, title="run me on the banned one", assignee="worker",
                           model_override=BANNED)
    assert "denied_model:deepseek-v4-pro" in str(excinfo.value)


def test_a_card_may_not_pin_a_denied_model_through_an_ALIAS(kanban_home, fake_denial_board):
    with kbc.connect() as conn:
        with pytest.raises(ModelDeniedError):
            kb.create_task(conn, title="alias attempt", assignee="worker",
                           model_override="deepseek/DeepSeek-V4-Pro-0813")


def test_setting_the_override_later_still_refuses(kanban_home, fake_denial_board):
    """The dashboard/CLI ``set_model_override`` path is the same chokepoint."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="starts clean", assignee="worker")
        with pytest.raises(ModelDeniedError):
            kb.set_model_override(conn, tid, BANNED)


def test_a_permitted_override_is_untouched(kanban_home, fake_denial_board):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fine", assignee="worker", model_override="deepseek-flash")
        task = kb.get_task(conn, tid)
        assert task is not None and task.model_override == "deepseek-flash"
    assert fake_denial_board.calls == []


def test_the_refusal_routes_to_the_lane_design_authority(kanban_home, fake_denial_board):
    """A pin is a lane misconfiguration, so the card goes to the lane's ``<domain>-stl``."""
    with kbc.connect() as conn:
        with pytest.raises(ModelDeniedError):
            kb.create_task(conn, title="pinned", assignee="research-coder", model_override=BANNED)
    assert len(fake_denial_board.calls) == 1
    assert fake_denial_board.calls[0]["assignee"] == "research-stl"
