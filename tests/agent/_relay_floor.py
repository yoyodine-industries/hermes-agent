"""The nemo-relay version floor, as the suite enforces it.

Kept in its own module (not in ``test_relay_llm.py``) for one reason: that test module ends in
a module-level ``pytest.skip`` when the relay cannot be exercised, and a skip declared there
would swallow any test of the guard itself, which is exactly the case worth proving.

THE FLOOR IS ENFORCED, THE CEILING IS NOT - deliberately, and this is the decision record.

``pyproject.toml`` declares ``nemo-relay>=0.10,<0.11`` for ``python_version >= '3.14'``. This
module enforces only the lower bound, and the asymmetry is the point:

* BELOW the floor the environment cannot exercise the API these tests use. A *named skip* is
  the honest reading: nothing about the change under test is in question.
* AT OR ABOVE the ceiling the pin itself has drifted - somebody bumped the requirement past
  what this suite was written against. Skipping there would hide a deliberate change that is
  ours to notice, so above the ceiling the tests RUN, and if they then fail that failure is
  real information rather than noise to be suppressed.
"""

from __future__ import annotations

import importlib.util
import re

#: The API the relay tests exercise: the lower bound pyproject.toml declares, except that
#: pyproject gates it behind ``python_version >= '3.14'`` and this does not, because the tests
#: need the API on whatever interpreter runs them.
RELAY_FLOOR: tuple[int, int] = (0, 10)

#: The upper bound pyproject.toml declares. NOT enforced here (see the module docstring): above
#: it the pin itself has drifted and the tests should say so by running, not by skipping.
RELAY_CEILING: tuple[int, int] = (0, 11)

#: Why the module is skipped when the package is not installed at all. Named, not pytest's bare
#: default: the one environment where the pyproject requirement is ACTIVE (python >= 3.14) was
#: also the one with no wheel installed, so a silent skip there reads as "not needed".
ABSENCE_REASON = (
    "nemo-relay absent; pyproject declares >=0.10 on python>=3.14 "
    "(provisioning is tracked separately)"
)


def version_tuple(text: str) -> tuple[int, ...]:
    """`"0.10.3"` -> `(0, 10)`. Only the first two components decide the floor."""
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:2])


def relay_available() -> bool:
    """True when ``nemo_relay`` can be imported - decided without importing it."""
    try:
        return importlib.util.find_spec("nemo_relay") is not None
    except (ImportError, ValueError):
        return False


def installed_relay_version() -> str:
    """The installed ``nemo-relay`` version, or ``""`` when it cannot be read.

    Read from the distribution metadata, NOT from the module: ``nemo_relay`` exposes no
    ``__version__`` attribute (measured on 0.10.3), so ``pytest.importorskip(minversion=...)``
    cannot be used here - it would raise on the missing attribute instead of skipping.
    """
    try:
        from importlib.metadata import version

        return version("nemo-relay")
    except Exception:
        return ""


def below_relay_floor(installed: str, floor: tuple[int, ...] = RELAY_FLOOR) -> bool:
    """True when ``installed`` is older than ``floor`` - including when it is unreadable.

    An unknown version cannot certify the API the tests need, so it is treated as below the
    floor: the suite skips and SAYS SO, rather than running and failing for a reason that is
    not the code's.
    """
    got = version_tuple(installed)
    if not got:
        return True
    return got < floor


def skip_reason(available: bool | None = None, installed: str | None = None) -> str | None:
    """The reason this module must skip, or ``None`` when the relay can be exercised.

    One function, so the decision is testable without a skip: the module-level call site is
    then three lines that cannot drift from what the tests assert.
    """
    if available is None:
        available = relay_available()
    if not available:
        return ABSENCE_REASON
    if installed is None:
        installed = installed_relay_version()
    if below_relay_floor(installed):
        floor = ".".join(str(n) for n in RELAY_FLOOR)
        return (
            f"nemo-relay {installed or 'of unknown version'} is below the "
            f">={floor} floor these tests exercise"
        )
    return None
