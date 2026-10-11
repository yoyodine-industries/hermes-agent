"""No catalog path may offer a denied model (card t_a1ae7cc6, acceptance case A1).

These ask the PUBLIC functions, not the helper, because the requirement is about what a picker
receives. The boundary filter is deliberate: both catalog functions have ~10 return paths (live
fetch, warm disk cache, static fallback, relay probe), and filtering the boundary is what makes
"every return path, including the cache hit" true by construction rather than by inspection.

The three paths the ruling names are covered explicitly below: the LIVE fetch succeeding, the
fetch FAILING (the offline/curated tail), and a WARM ``provider_models_cache.json``.
"""

from __future__ import annotations

import time

from hermes_cli import models as M
from hermes_cli.models import (
    _chat_catalog_rows,
    cached_provider_model_ids,
    curated_models_for_provider,
    provider_model_ids,
)

BANNED = "deepseek-v4-pro"


def _no_relay(monkeypatch):
    """Force the fetcher path: a configured relay would be terminal for catalog egress."""
    monkeypatch.setattr(M, "_configured_relay_base_url", lambda provider: "")


# --------------------------------------------------------------------------- #
# the boundary filter itself
# --------------------------------------------------------------------------- #

def test_the_boundary_filter_drops_a_denied_id_in_BOTH_shapes():
    """The two shapes a catalog path can produce: (id, description) rows and bare ids."""
    rows = _chat_catalog_rows([("deepseek-v4-pro", "the banned one"), ("deepseek-flash", "fine")])
    assert [r[0] for r in rows] == ["deepseek-flash"], rows
    ids = _chat_catalog_rows(["deepseek-v4-pro", "deepseek-flash"])
    assert list(ids) == ["deepseek-flash"], ids


def test_the_boundary_filter_drops_an_ALIAS_too():
    """A cache or a vendor list may spell it differently; the filter must still drop it."""
    assert list(_chat_catalog_rows(["deepseek/DeepSeek-V4-Pro-0813"])) == []
    assert list(_chat_catalog_rows([("DeepSeek-V4-Pro-0813", "banned")])) == []


def test_the_boundary_filter_tolerates_empty_and_odd_input():
    assert list(_chat_catalog_rows([])) == []
    assert list(_chat_catalog_rows([("deepseek-flash",)])) == [("deepseek-flash",)]


# --------------------------------------------------------------------------- #
# A1 path 1: the LIVE fetch SUCCEEDS and returns the banned id
# --------------------------------------------------------------------------- #

def test_a_live_fetch_that_returns_the_banned_id_still_does_not_offer_it(monkeypatch):
    _no_relay(monkeypatch)
    monkeypatch.setitem(
        M._PROVIDER_CATALOG_FETCHERS, "deepseek",
        lambda normalized, force_refresh=False: ["deepseek-v4-pro", "deepseek-flash"],
    )
    ids = provider_model_ids("deepseek")
    assert ids == ["deepseek-flash"], ids
    assert not [m for m in ids if "v4-pro" in m.lower()], ids


def test_a_live_fetch_reached_through_the_curated_catalog_drops_it_too(monkeypatch):
    _no_relay(monkeypatch)
    monkeypatch.setitem(
        M._PROVIDER_CATALOG_FETCHERS, "deepseek",
        lambda normalized, force_refresh=False: ["deepseek/DeepSeek-V4-Pro-0813", "deepseek-flash"],
    )
    rows = curated_models_for_provider("deepseek")
    listed = [row[0] for row in rows if isinstance(row, tuple) and row]
    assert "deepseek/DeepSeek-V4-Pro-0813" not in listed, listed
    assert not [m for m in listed if "v4-pro" in m.lower()], listed


# --------------------------------------------------------------------------- #
# A1 path 2: the fetch FAILS (offline) -> the curated/static tail
# --------------------------------------------------------------------------- #

def test_the_offline_curated_tail_never_names_the_banned_model(monkeypatch):
    _no_relay(monkeypatch)
    monkeypatch.setitem(M._PROVIDER_CATALOG_FETCHERS, "deepseek",
                        lambda normalized, force_refresh=False: None)  # fetch declined
    monkeypatch.setattr(M, "_profile_live_catalog", lambda normalized: None)

    ids = provider_model_ids("deepseek")
    assert isinstance(ids, list) and ids, "the curated tail must still serve a catalog"
    assert not [m for m in ids if "v4-pro" in m.lower()], ids

    rows = curated_models_for_provider("deepseek")
    listed = [row[0] for row in rows if isinstance(row, tuple) and row]
    assert not [m for m in listed if "v4-pro" in m.lower()], listed


# --------------------------------------------------------------------------- #
# A1 path 3: a WARM provider_models_cache.json
# --------------------------------------------------------------------------- #

def test_a_warm_disk_cache_may_not_re_serve_the_banned_model(monkeypatch):
    """A 1h-TTL cache read is a return path too: whoever wrote the row cannot undo the ban."""
    fp = "fingerprint-under-test"
    monkeypatch.setattr(M, "_credential_fingerprint", lambda provider: fp)
    monkeypatch.setattr(M, "_load_provider_models_cache", lambda: {
        "deepseek": {"fp": fp, "at": time.time(), "models": [BANNED, "deepseek-flash"]},
    })

    ids = cached_provider_model_ids("deepseek")
    assert "deepseek-flash" in ids, ids
    assert BANNED not in ids, ids
    assert not [m for m in ids if "v4-pro" in m.lower()], ids


def test_the_warm_cache_never_re_serves_an_ALIAS_either(monkeypatch):
    fp = "fingerprint-under-test"
    monkeypatch.setattr(M, "_credential_fingerprint", lambda provider: fp)
    monkeypatch.setattr(M, "_load_provider_models_cache", lambda: {
        "deepseek": {"fp": fp, "at": time.time(), "models": ["deepseek/DeepSeek-V4-Pro-0813"]},
    })
    assert cached_provider_model_ids("deepseek") == []
