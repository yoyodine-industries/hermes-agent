"""The fleet's model denylist — ONE predicate, so no surface can offer a banned model.

DeepSeek V4 Pro is BANNED fleet-wide (operator, 2026-10-10: it "broke the bank", and the ban is
about COST, not capability). A ban that lives only in a document, or only in each surface, is not a
ban: it drifts, and the surface that forgets is the one that spends. So there is one predicate
here, :func:`denial` / :func:`is_denied`, and every chokepoint calls it instead of keeping a list
of its own (design ruling, card t_88114aaf, section B2: "no second list anywhere").

Two layers, and only one of them is configurable (ruling B1):

* the BUILD FLOOR — :data:`DENIED_MODELS`, in code. It cannot be removed by configuration;
  dropping an id from it is a code change, which is the point of a durable guard.
* the CONFIG LAYER — ``model.denied_models: [...]``, ADD-ONLY. A config may add ids and can never
  subtract from the floor.

Matching normalizes case, a vendor prefix (``deepseek/…``) and a date suffix (``…-0813``) — the
same three normalizations :mod:`hermes_cli.model_normalize` applies — so an alias cannot slip past
the floor by being spelled differently. The normalization is implemented here rather than imported
from that module's private helpers, so this predicate has no import-time dependency on it.

The refusal is HARD. :func:`assert_allowed` raises :class:`ModelDeniedError`; there is no
"confirm past it" path and no fallback model, because a silent fallback still spends — which is
the entire defect this ban exists to stop (ruling B3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: The build floor: canonical ids and their aliases. Removing one is a CODE change by design.
DENIED_MODELS = frozenset({
    "deepseek-v4-pro",
    "deepseek-v4-pro-0813",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-pro-0813",
})

#: The config layer's key. ADD-ONLY; it can never shrink the floor above.
DENIED_MODELS_CONFIG_KEY = "model.denied_models"

#: A trailing date stamp such as ``-0813`` (or a longer ``-20250813``).
_DATE_SUFFIX_RE = re.compile(r"-\d{4,8}$")


def canonical(model: Any) -> str:
    """The comparison form of a model id: lowercase, no vendor prefix, no date suffix.

    ``deepseek/DeepSeek-V4-Pro-0813`` -> ``deepseek-v4-pro``, which is the floor's entry. Dots
    become hyphens so ``deepseek.v4.pro`` reads as the same model.
    """
    name = str(model or "").strip().lower()
    if not name:
        return ""
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    name = name.replace(".", "-")
    return _DATE_SUFFIX_RE.sub("", name)


_CANONICAL_FLOOR = frozenset(canonical(entry) for entry in DENIED_MODELS)


@dataclass(frozen=True)
class Denial:
    """Why one model id is refused, and which layer refused it."""

    model: str
    canonical_id: str
    source: str  # "build_floor" | "config"
    reason: str = "denied_model"

    @property
    def machine_reason(self) -> str:
        """The refusal's machine-readable reason, e.g. ``denied_model:deepseek-v4-pro``."""
        return "%s:%s" % (self.reason, self.canonical_id)


class ModelDeniedError(RuntimeError):
    """An effective model is on the denylist. Raised, never downgraded to a warning."""

    def __init__(self, denial: Denial, where: str = ""):
        self.denial = denial
        self.where = where
        detail = " at %s" % where if where else ""
        super().__init__(
            "the model %r is DENIED fleet-wide (%s: %s)%s - it must not be selected, resolved or "
            "run, and no fallback model may be substituted: refusing rather than spending"
            % (denial.model, denial.source, denial.machine_reason, detail)
        )


def _config_layer(config: Optional[Mapping[str, Any]] = None) -> frozenset:
    """The configured additions. A missing/odd config is EMPTY, never an error: the floor stands.

    Reading the live config is best-effort by design — a config that cannot be loaded must not
    disable the floor, and must not crash a picker. Anything unreadable simply contributes nothing.
    """
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly as _load

            config = _load()
        except Exception:
            return frozenset()
    try:
        raw = config.get("model", {}).get("denied_models") or []
    except (AttributeError, TypeError):
        return frozenset()
    if isinstance(raw, str):
        raw = [raw]
    try:
        return frozenset(canonical(item) for item in raw if str(item or "").strip())
    except TypeError:
        return frozenset()


def effective_denied(config: Optional[Mapping[str, Any]] = None) -> frozenset:
    """The effective deny-set: the build floor UNION the config layer."""
    return _CANONICAL_FLOOR | _config_layer(config)


def denial(model: Any, provider: Any = None, config: Optional[Mapping[str, Any]] = None) -> Optional[Denial]:
    """``Denial`` when *model* is banned, else ``None``. The ONE predicate (ruling B2)."""
    canonical_id = canonical(model)
    if not canonical_id:
        return None
    if canonical_id in _CANONICAL_FLOOR:
        source = "build_floor"
    elif canonical_id in _config_layer(config):
        source = "config"
    else:
        return None
    return Denial(model=str(model), canonical_id=canonical_id, source=source)


def is_denied(model: Any, provider: Any = None, config: Optional[Mapping[str, Any]] = None) -> bool:
    """True when *model* is banned. A boolean shortcut over :func:`denial`."""
    return denial(model, provider=provider, config=config) is not None


def assert_allowed(model: Any, provider: Any = None, config: Optional[Mapping[str, Any]] = None,
                   where: str = "") -> None:
    """Raise :class:`ModelDeniedError` when *model* is banned; otherwise return None."""
    found = denial(model, provider=provider, config=config)
    if found is not None:
        raise ModelDeniedError(found, where=where)
