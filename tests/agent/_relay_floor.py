"""The nemo-relay version floor, as the suite enforces it.

Kept in its own module (not in ``test_relay_llm.py``) for one reason: that test module ends in
a module-level ``pytest.skip`` when the installed relay is too old, and a skip declared there
would swallow any test of the guard itself, which is exactly the case worth proving.
"""

from __future__ import annotations

import re

#: The API the relay tests exercise. nemo-relay >= 0.10, < 0.11 - the same window pyproject.toml
#: declares, except that pyproject gates it behind ``python_version >= '3.14'`` and this does not,
#: because the tests need the API on whatever interpreter runs them.
RELAY_FLOOR: tuple[int, int] = (0, 10)


def version_tuple(text: str) -> tuple[int, ...]:
    """`"0.10.3"` -> `(0, 10)`. Only the first two components decide the floor."""
    return tuple(int(n) for n in re.findall(r"\d+", text or "")[:2])


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
