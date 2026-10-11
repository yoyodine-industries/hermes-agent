"""No catalog path may offer a denied model (card t_a1ae7cc6, acceptance case A1).

These ask the PUBLIC functions, not the helper, because the requirement is about what a picker
receives. The boundary filter is deliberate: both catalog functions have ~10 return paths (live
fetch, warm disk cache, static fallback, relay probe), and filtering the boundary is what makes
"every return path, including the cache hit" true by construction rather than by inspection.
"""

from __future__ import annotations

from hermes_cli.models import (
    _chat_catalog_rows,
    curated_models_for_provider,
    provider_model_ids,
)

BANNED = "deepseek-v4-pro"


def test_the_provider_ids_never_include_a_denied_model():
    ids = provider_model_ids("deepseek")
    assert isinstance(ids, list)
    assert BANNED not in ids, ids
    assert not [m for m in ids if "v4-pro" in m.lower()], ids


def test_the_curated_catalog_never_lists_a_denied_model():
    rows = curated_models_for_provider("deepseek")
    assert isinstance(rows, list)
    listed = [row[0] for row in rows if isinstance(row, tuple) and row]
    assert BANNED not in listed, listed
    assert not [m for m in listed if "v4-pro" in m.lower()], listed


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
