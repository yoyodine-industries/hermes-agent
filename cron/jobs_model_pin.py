"""The main agent's current ``(provider, model)``, which ``pinned=True`` locks onto a cron job."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home


def _model_name_from_block(model_cfg: Any) -> str:
    """The model NAME a ``model:`` config block names: ``model: <name>`` shorthand, or a dict's
    ``default`` / ``model`` / ``name`` key. ``""`` when it names none."""
    from cron.jobs import _normalize_job_optional_text  # late: the facade imports this module

    if isinstance(model_cfg, str):
        return model_cfg.strip()
    if isinstance(model_cfg, dict):
        return _normalize_job_optional_text(
            model_cfg.get("default") or model_cfg.get("model") or model_cfg.get("name")) or ""
    return ""


def _load_home_model_block(home: Path) -> Any:
    """``model:`` block of *home*'s ``config.yaml`` ({} when absent/empty/unreadable).

    A NAMED profile's home is self-contained: the effective-config loader reads only that home's
    ``config.yaml``, so a profile with no ``model:`` block has no default model of its own.
    """
    from hermes_cli.config_effective import load_user_config_effective

    cfg_path = Path(home) / "config.yaml"
    if not cfg_path.exists():
        return {}
    with contextlib.suppress(Exception):
        cfg = load_user_config_effective(cfg_path)
        if isinstance(cfg, dict):
            return cfg.get("model") or {}
    return {}


def host_default_model(active_home: Optional[Path] = None) -> tuple[str, str, Any]:
    """``(model, provider, model_cfg)`` a NAMED profile inherits from the HOST root config.

    The active home's own config is the first source (see ``_main_model_pin`` / the scheduler); when
    it carries no ``model:`` block, a named profile (``<root>/profiles/<name>``) falls back to the
    HOST root's ``<root>/config.yaml`` default — the same default the root profile runs on — so a
    pinned cron job created in that profile's store RUNS instead of dying with "has no model
    configured" (card t_92b4a684). ``("", "", {})`` when the active home IS the root, or when the
    host config names no model either.
    """
    from cron.jobs import _normalize_job_optional_text  # late: the facade imports this module
    from hermes_constants import get_default_hermes_root

    active = Path(active_home) if active_home is not None else get_hermes_home()
    try:
        root = Path(get_default_hermes_root(home=active))
        same_root = root.resolve() == active.resolve()
    except OSError:  # pragma: no cover - unreadable path
        return "", "", {}
    if same_root:
        return "", "", {}
    block = _load_home_model_block(root)
    model = _model_name_from_block(block)
    if not model:
        return "", "", {}
    provider = ""
    if isinstance(block, dict):
        provider = _normalize_job_optional_text(block.get("provider")) or ""
    return model, provider, block


def _main_model_pin() -> tuple[Optional[str], Optional[str]]:
    """``(provider, model)`` the main agent runs on right now (``model.default`` + the provider it
    resolves to), for ``pinned=True`` jobs: the lock is a plain per-job pin, so the scheduler needs
    no second precedence axis. ``(None, None)`` when nothing is configured (the job stays unpinned).

    A NAMED profile with no ``model:`` of its own inherits the HOST root default
    (``host_default_model``): answering "nothing configured" there would create a silently-dead
    pinned job, which the caller refuses instead (card t_92b4a684).

    ``ValueError`` when the config backend cannot serve the profile (``ConfigBackendUnavailable``):
    answering "nothing configured" would silently drop the lock the caller asked for, so the
    create/update is refused like any other invalid input, and the caller's process lives on."""
    from cron.jobs import _normalize_job_optional_text  # late: the facade imports this module
    from hermes_cli.config_backend import ConfigBackendUnavailable, config_exists
    from hermes_cli.config_effective import load_user_config_effective

    home = get_hermes_home()
    cfg_path = home / "config.yaml"
    try:
        cfg = load_user_config_effective(cfg_path) if config_exists(cfg_path) else {}
    except ConfigBackendUnavailable as exc:
        raise ValueError(f"cannot pin the job to the main model: {exc.code}") from exc
    model_cfg = cfg.get("model") or {}
    requested: Optional[str] = None
    model = _model_name_from_block(model_cfg)
    if not model:
        _host_model, _host_provider, _host_cfg = host_default_model(home)
        if _host_model:
            model = _host_model
            model_cfg = _host_cfg
            requested = _host_provider or None
    if not model:
        return None, None
    provider = None
    with contextlib.suppress(Exception):
        from hermes_cli.runtime_provider import resolve_runtime_provider
        provider = _normalize_job_optional_text(
            resolve_runtime_provider(requested=requested).get("provider"))
    return (provider.lower() if provider else None), model
