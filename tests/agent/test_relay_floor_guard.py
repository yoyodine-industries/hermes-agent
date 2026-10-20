"""The relay-floor guard itself: stale, absent and fresh versions must be distinguishable.

These run in their own module so the module-level skip in ``test_relay_llm.py`` cannot hide
them - the case worth proving is exactly the one that module skips.
"""

from __future__ import annotations

import pytest

from tests.agent._relay_floor import (
    RELAY_FLOOR,
    below_relay_floor,
    installed_relay_version,
    version_tuple,
)


def test_the_floor_matches_the_declared_window() -> None:
    assert RELAY_FLOOR == (0, 10)


@pytest.mark.parametrize("version", ["0.8.3", "0.9.9", "0.1", "0.0.0"])
def test_a_stale_relay_is_below_the_floor(version: str) -> None:
    # 0.8.3 is the exact wheel that turned this suite red on a 3.11 venv (card t_473047e1).
    assert below_relay_floor(version) is True


@pytest.mark.parametrize("version", ["0.10", "0.10.0", "0.10.3", "0.11.0", "1.0"])
def test_a_current_relay_is_not_below_the_floor(version: str) -> None:
    assert below_relay_floor(version) is False


@pytest.mark.parametrize("version", ["", "unknown", "not-a-version"])
def test_an_unreadable_version_skips_rather_than_passes(version: str) -> None:
    # A version we cannot read cannot certify the API the tests need: skip and say so, never
    # run and fail for a reason that is not the code's.
    assert below_relay_floor(version) is True


def test_version_parsing_covers_prereleases_and_suffixes() -> None:
    assert version_tuple("0.10.3rc1") == (0, 10)
    assert version_tuple("0.8.3.dev0") == (0, 8)
    assert version_tuple("v0.10") == (0, 10)


def test_the_installed_version_reader_never_raises() -> None:
    # It is called at import time of the relay suite, so it must degrade to "" rather than
    # raise and turn a skip into a collection error.
    assert isinstance(installed_relay_version(), str)
