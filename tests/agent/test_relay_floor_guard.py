"""The relay-floor guard itself: stale, absent and fresh cases must be distinguishable.

These run in their own module so the module-level skip in ``test_relay_llm.py`` cannot hide
them - the case worth proving is exactly the one that module skips.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests.agent._relay_floor import (
    ABSENCE_REASON,
    RELAY_CEILING,
    RELAY_FLOOR,
    below_relay_floor,
    installed_relay_version,
    skip_reason,
    version_tuple,
)

try:  # py3.11+ ships tomllib; older interpreters need the backport
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - depends on the interpreter
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        tomllib = None  # type: ignore[assignment]


def _declared_relay_requirements() -> list[str]:
    """Every nemo-relay requirement string pyproject.toml declares, wherever it sits."""
    if tomllib is None:  # pragma: no cover
        pytest.skip("no TOML parser available on this interpreter to read pyproject.toml")
    assert tomllib is not None  # narrowing for the type checker; the skip above returns
    root = pathlib.Path(__file__).resolve().parents[2]
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    found: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            if node.strip().startswith("nemo-relay"):
                found.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(data)
    return found


def test_the_floor_matches_the_declared_window() -> None:
    """The duplicate is PINNED, not restated: the constant is checked against pyproject.toml.

    Asserting ``RELAY_FLOOR == (0, 10)`` alone compares a constant with itself and cannot catch
    the drift its name claims to guard - this reads the requirement out of the file instead.
    """
    requirements = _declared_relay_requirements()
    assert requirements, "pyproject.toml declares no nemo-relay requirement to pin against"

    lower_bounds = set()
    for requirement in requirements:
        match = re.search(r"nemo-relay>=\s*([0-9][0-9.]*)", requirement)
        if match:
            lower_bounds.add(version_tuple(match.group(1)))
    assert lower_bounds, f"no lower bound found in the declared requirements: {requirements}"
    assert lower_bounds == {RELAY_FLOOR}, (
        "tests/agent/_relay_floor.py RELAY_FLOOR "
        f"{RELAY_FLOOR} has drifted from the declared lower bound(s) {sorted(lower_bounds)}"
    )


def test_the_ceiling_is_the_declared_one_and_is_not_enforced() -> None:
    """The asymmetry is deliberate: the ceiling is recorded here, never applied as a skip.

    Above the ceiling the pin itself drifted - that is a change of ours to notice, so the tests
    run there rather than skipping. This test pins the ceiling value AND the non-enforcement.
    """
    requirements = _declared_relay_requirements()
    upper_bounds = set()
    for requirement in requirements:
        match = re.search(r"nemo-relay[^;]*?<\s*([0-9][0-9.]*)", requirement)
        if match:
            upper_bounds.add(version_tuple(match.group(1)))
    assert upper_bounds == {RELAY_CEILING}
    for above in ("0.11.0", "0.12.0", "1.0"):
        assert below_relay_floor(above) is False, (
            f"{above} must not be skipped - it is above the ceiling, where a skip would hide the drift"
        )
        assert skip_reason(available=True, installed=above) is None


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


def test_absence_names_itself_and_the_requirement() -> None:
    """A missing package must not skip with pytest's bare default reason.

    The one environment where the pyproject requirement is ACTIVE (python >= 3.14) was also the
    one with no wheel installed, so a silent skip reads as "not needed" exactly where it is
    needed. The reason has to say the package is absent and name the declared floor.
    """
    reason = skip_reason(available=False, installed="")
    assert reason == ABSENCE_REASON
    assert "absent" in reason
    assert ">=0.10" in reason
    assert "install" in reason, "the skip must say what to install, not just what is missing"


def test_a_stale_version_names_the_version_and_the_floor() -> None:
    reason = skip_reason(available=True, installed="0.8.3")
    assert reason is not None
    assert "0.8.3" in reason
    assert "0.10" in reason
    assert "install" in reason, "a stale environment must name the install that fixes it"


def test_a_fresh_relay_is_not_skipped() -> None:
    assert skip_reason(available=True, installed="0.10.3") is None
