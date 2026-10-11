"""The per-board FLOOR in the global-priority host allocator (card t_44485b2b).

``host_budget_shares_by_priority`` (gateway/kanban_watchers_dispatcher.py) hands the free
host budget out by GLOBAL CARD PRIORITY. Priority alone is not enough to satisfy "no board
is starved while another holds the slots": when one board's whole queue sits in a higher
band than another board's work, the fill pass serves that one board until ``free`` is gone
and the lower board never runs at all.

So the allocator carries a FLOOR pass, taken BEFORE the fill: every board holding claimable
work is guaranteed one slot, bounded by ``free`` and by that board's own remaining capacity,
handed out in priority order when ``free`` is smaller than the number of waiting boards. It
is the same one-slot-per-board property the settled ceiling allocator
(``host_budget_shares_by_ceiling``) already asserts for itself
(``(2, ["a", "b"], {"a": 5, "b": 5}) == {"a": 1, "b": 1}``).

This file pins the floor AND re-asserts the five settled pure-allocator scenarios, so the
floor can never be landed at the cost of the behaviour those cases fixed.
"""

from __future__ import annotations

from pathlib import Path

import gateway.kanban_watchers_dispatcher as kwd


def _ids_tree() -> Path:
    import gateway

    return Path(gateway.__file__).resolve().parents[1]


def test_allocator_resolves_to_this_tree():
    import gateway

    assert Path(gateway.__file__).resolve().parents[1] == Path(kwd.__file__).resolve().parents[1]
    assert _ids_tree() == Path(kwd.__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The floor: a lower band is not starved by a deeper higher-band queue
# ---------------------------------------------------------------------------


def test_floor_gives_a_lower_band_board_one_slot_before_the_fill_takes_everything():
    """The defect this file exists for: 8 free, a deep 999000 queue, one 800000 card.

    Without the floor the fill serves ``defcon`` until the budget is gone and ``ops``
    gets nothing, however long its one card waits. With the floor ``ops`` runs every tick.
    """
    cards = [("999000", "defcon")] * 20 + [("800000", "ops")]

    shares = kwd.host_budget_shares_by_priority(8, cards, {"defcon": 8, "ops": 8})

    assert shares == {"defcon": 7, "ops": 1}
    assert shares.get("ops") == 1, "the lower board must run — this is the starvation fix"


def test_floor_is_handed_out_in_priority_order_when_free_is_smaller_than_the_waiters():
    """Three boards waiting, two slots: the top two by best card take them."""
    cards = (
        [("999000", "a")] * 5 + [("800000", "b")] * 5 + [("700000", "c")] * 5
    )

    assert kwd.host_budget_shares_by_priority(2, cards, None) == {"a": 1, "b": 1}


def test_floor_never_exceeds_a_boards_own_capacity():
    """A board at ceiling 0 (or at its ceiling already) draws no floor slot."""
    cards = [("999000", "a")] * 5 + [("999000", "b")] * 5

    # ``a`` cannot take anything, so the whole floor goes to ``b``.
    assert kwd.host_budget_shares_by_priority(2, cards, {"a": 0, "b": 5}) == {"b": 2}


def test_floor_does_not_double_count_a_boards_only_card():
    """A one-card board ends with ONE slot, never one from the floor plus one from the fill."""
    cards = [("999000", "a")] * 4 + [("800000", "b")]

    assert kwd.host_budget_shares_by_priority(10, cards, None) == {"a": 4, "b": 1}


# ---------------------------------------------------------------------------
# The five settled pure-allocator cases, unchanged by the floor
# ---------------------------------------------------------------------------


def test_settled_case_one_higher_band_wins_the_remainder():
    cards = [("999000", "defcon")] * 8 + [("999999", "ops")]

    assert kwd.host_budget_shares_by_priority(10, cards, {"defcon": 8, "ops": 2}) == {
        "defcon": 8, "ops": 1,
    }


def test_settled_case_two_one_slot_goes_to_the_higher_band():
    cards = [("999000", "defcon")] * 8 + [("999999", "ops")]

    assert kwd.host_budget_shares_by_priority(1, cards, None) == {"ops": 1}


def test_settled_case_three_equal_bands_round_robin_to_capacity():
    cards = [("0", "a")] * 20 + [("0", "b")] * 20

    assert kwd.host_budget_shares_by_priority(10, cards, {"a": 8, "b": 2}) == {"a": 8, "b": 2}


def test_settled_case_four_capped_board_neither_consumes_nor_strands():
    cards = [("0", "a")] * 20 + [("0", "b")] * 3

    assert kwd.host_budget_shares_by_priority(10, cards, {"a": 2, "b": 8}) == {"a": 2, "b": 3}


def test_settled_case_five_no_free_budget_allocates_nothing():
    assert kwd.host_budget_shares_by_priority(0, [("0", "a")], None) == {}
    assert kwd.host_budget_shares_by_priority(None, [("0", "a")], None) == {}
