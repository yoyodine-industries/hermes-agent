"""Cross-tier starvation: the age credit is TIER PROMOTION, not a 6-point nudge.

Card t_a0678a13, implementing the fairness spec designed in t_af1008bb.

The defect: ``priority_age_bonus`` capped a card's waiting-time credit at SIX
POINTS while the live ladder is spaced 10 000 points per tier, so the credit
could only reorder cards already inside one band. A card on a lower band could
never overtake a fresher card on a higher one, however long it waited — measured
live 2026-10-25: the oldest ready card on the platform board ranked 4 146 of
4 777 (87 % deep).

The rule (loaded from ``kanban_priority_policy.ORDINARY_MAX``, never re-spelled)::

    credit(p, created_at) = min(max(0, CEILING - p),
                                RUNG_POINTS * max(0, floor(wait / RUNG_SECONDS)))
    effective             = p + credit(p, created_at)

with ``RUNG_POINTS = 10_000`` (one sub-band) and ``RUNG_SECONDS = 3600``. The cap
is PER CARD, which is what keeps an admission above the ladder at credit 0.

The acceptance tests below are the seven named in the card, in order.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest

# One clock for the whole file: ages are expressed relative to it, so the
# fixtures stay true as real time passes.
NOW = int(time.time())

RUNG_POINTS = 10_000
RUNG_SECONDS = 3600
HOUR = 3600
DAY = 24 * HOUR


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from hermes_cli import kanban_db as kb

    kb.init_db()
    return home


@pytest.fixture()
def all_assignees_spawnable(monkeypatch):
    """Every ``profile_exists`` probe answers True (the dispatcher's spawn gate)."""
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@contextlib.contextmanager
def _open(board=None):
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing(board=board) as conn:
        yield conn


def _board(conn, slug: str = "default") -> None:
    from hermes_cli import kanban_db as kb

    kb.create_board(slug=slug, name=f"Cross-tier {slug}")


def _card(
    card_id: str,
    *,
    priority: int,
    seconds_ago: float = 0.0,
    assignee: str = "platform-coder",
    status: str = "ready",
    board=None,
) -> str:
    """Create one card with a controlled DECLARED priority and age.

    ``create_task`` owns the write door (so the domain/floor guards run); the age
    is then stamped, because waiting time is the variable under test and SQLite
    stores ``created_at`` in whole seconds.
    """
    from hermes_cli import kanban_db as kb

    with _open(board) as conn:
        tid = kb.create_task(
            conn, title=card_id, assignee=assignee, priority=priority, board=board
        )
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (NOW - int(seconds_ago), tid)
        )
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
        conn.commit()
    return tid


def _ids(rows) -> list:
    return [row["id"] for row in rows]


# ---------------------------------------------------------------------------
# 1. One rung crosses a sub-band; the aged card wins the tie
# ---------------------------------------------------------------------------


def test_one_rung_crosses_a_sub_band_and_the_waiting_card_wins_the_tie():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_priority_policy as policy

    ceiling = int(policy.ORDINARY_MAX)

    # One hour of waiting is ONE RUNG — the sub-band gap — not one point.
    assert kb.PRIORITY_AGE_CREDIT_RUNG_SECONDS == RUNG_SECONDS
    assert kb.PRIORITY_AGE_CREDIT_RUNG_POINTS == RUNG_POINTS
    assert kb.effective_priority(890_000, NOW - HOUR, now=NOW) == ceiling
    # ... which is exactly the fresh top band's value: they TIE, and the tie
    # falls to the older card.
    assert kb.effective_priority(890_000, NOW - HOUR, now=NOW) == kb.effective_priority(
        ceiling, NOW, now=NOW
    )
    # The credit is the rung count in POINTS, and the ceiling is per card.
    assert kb.priority_age_bonus(NOW - 3 * HOUR, now=NOW) == 3 * RUNG_POINTS
    assert kb.priority_age_credit(890_000, NOW - 3 * HOUR, now=NOW) == RUNG_POINTS
    assert kb.effective_priority(890_000, NOW - 99 * HOUR, now=NOW) == ceiling

    # A whole band lower needs ELEVEN rungs (11 h), not 11 000 hours: at ten
    # hours it is still one rung short of the top of the ordinary domain.
    assert kb.effective_priority(790_000, NOW - 10 * HOUR, now=NOW) == 890_000
    assert kb.effective_priority(790_000, NOW - 11 * HOUR, now=NOW) == ceiling


def test_the_waiting_lower_band_card_is_dispatched_first(
    kanban_home, all_assignees_spawnable
):
    """The lane the dispatcher spawns from AND the default listing order."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    with _open() as conn:
        _board(conn)
    fresh_top = _card("t_fresh_top", priority=900_000, seconds_ago=0)
    waited_rest = _card("t_waited_rest", priority=890_000, seconds_ago=HOUR)

    expected = [waited_rest, fresh_top]
    with _open() as conn:
        assert _ids(kbd._lane_rows(conn, "ready", now=NOW)) == expected
        assert [t.id for t in kb.list_tasks(conn, status="ready", now=NOW)] == expected


# ---------------------------------------------------------------------------
# 2. An admission is never promoted
# ---------------------------------------------------------------------------


def test_an_admission_is_never_promoted():
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_priority_policy as policy

    tenure = 365 * DAY
    for admission in (int(policy.TRANCHE_FLOOR), int(policy.TRANCHE_TOP)):
        # Credit 0 at ANY age: ``max(0, CEILING - p)`` is zero at or above the
        # ceiling, so no credit can lift a reserved-tranche card — a flat 10 000
        # cap would have sent 990000 to 1 000 000, above MAX_PRIORITY.
        assert kb.priority_age_credit(admission, NOW - tenure, now=NOW) == 0
        assert kb.effective_priority(admission, NOW - tenure, now=NOW) == admission
    assert int(policy.TRANCHE_TOP) == int(policy.MAX_PRIORITY)


def test_no_effective_value_escapes_the_ceiling(kanban_home):
    """No row's effective priority exceeds its declared band or the ceiling."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_priority_policy as policy

    with _open() as conn:
        _board(conn)
    _card("t_top", priority=900_000, seconds_ago=0)
    _card("t_rest", priority=890_000, seconds_ago=40 * DAY)
    _card("t_low", priority=690_000, seconds_ago=40 * DAY)
    _card("t_bottom", priority=-1000, seconds_ago=400 * DAY)

    ceiling = int(policy.ORDINARY_MAX)
    expr, params = kb.effective_priority_expression(NOW)
    with _open() as conn:
        rows = conn.execute(
            f"SELECT id, priority, {expr} AS eff_priority FROM tasks", params
        ).fetchall()
    assert len(rows) == 4
    for row in rows:
        assert int(row["priority"]) <= ceiling
        assert int(row["eff_priority"]) <= ceiling, "no credit may exceed the ceiling"
        assert int(row["eff_priority"]) >= int(row["priority"])


def test_a_designated_card_outranks_the_whole_ordinary_cohort(kanban_home):
    """The escape hatch survives: a DECLARED card beats every climbed card."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    with _open() as conn:
        _board(conn)
    climbed = _card("t_climbed", priority=-1000, seconds_ago=400 * DAY)
    with _open() as conn:
        designated = kb.create_task(conn, title="t_designated", assignee="platform-coder")
        kb.designate_priority(conn, designated, reason="cross-tier fairness test")
        conn.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?", (NOW, designated)
        )
        conn.commit()

    with _open() as conn:
        lane = _ids(kbd._lane_rows(conn, "ready", now=NOW))
    assert lane == [designated, climbed], "a fresh admission must head the lane"


# ---------------------------------------------------------------------------
# 3. The floored stratum is untouched (the t_3e6ff72e boundary)
# ---------------------------------------------------------------------------


def test_a_floored_stratum_orders_identically_under_both_keys(kanban_home):
    """Every card above the ceiling gets credit 0, so its order cannot move.

    Deliberately NOT fixed here: a floored board collapsing to one FIFO band is
    card t_3e6ff72e's own defect. This pins the boundary so that change stays
    possible without re-deriving this one.
    """
    from hermes_cli import kanban_db as kb

    with _open() as conn:
        _board(conn)
    ids = []
    for index in range(4):
        card = _card(f"t_floored_{index}", priority=0, seconds_ago=index * HOUR)
        with _open() as conn:
            kb.designate_priority(conn, card, reason="floored boundary test")
        ids.append(card)

    with _open() as conn:
        by_effective = [t.id for t in kb.list_tasks(conn, status="ready", now=NOW)]
        by_declared = [
            t.id for t in kb.list_tasks(conn, status="ready", order_by="priority", now=NOW)
        ]
        priorities = {row["id"]: int(row["priority"]) for row in conn.execute(
            "SELECT id, priority FROM tasks"
        )}
    assert priorities == {card: 990_000 for card in ids}
    assert by_effective == by_declared


# ---------------------------------------------------------------------------
# 4. A total order on identical created_at
# ---------------------------------------------------------------------------


def test_identical_created_at_falls_to_a_deterministic_id_order(kanban_home):
    """The ceiling is ONE BIG COHORT; a cohort needs a total order."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    with _open() as conn:
        _board(conn)
    # Same declared band, same second — and both far past the ceiling, which is
    # where live ties actually occur.
    first = _card("t_aaa", priority=890_000, seconds_ago=40 * DAY)
    second = _card("t_bbb", priority=890_000, seconds_ago=40 * DAY)

    with _open() as conn:
        seen = {tuple(_ids(kbd._lane_rows(conn, "ready", now=NOW))) for _ in range(5)}
        listed = [t.id for t in kb.list_tasks(conn, status="ready", now=NOW)]
    expected = tuple(sorted([first, second]))
    assert seen == {expected}, "an identical row set must order identically"
    assert tuple(listed) == expected, "ties resolve by id ASC, not arbitrarily"


# ---------------------------------------------------------------------------
# 5. The allocator bands on the effective key, not the declared one
# ---------------------------------------------------------------------------


def test_the_allocator_bands_on_the_effective_key(kanban_home, all_assignees_spawnable):
    """An aged lower-band head must not be re-demoted by the host allocator.

    ``tick_once`` used to label each row with its DECLARED priority, so a card
    the lane order had promoted to the head was handed to the allocator as its
    old band and lost to a fresher higher band on another board.
    """
    import gateway.kanban_watchers_dispatcher as kwd

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    with _open() as conn:
        _board(conn, "alpha")
    from hermes_cli import kanban_db as kb

    kb.create_board(slug="beta", name="Cross-tier beta")

    # alpha's head is a band lower but has climbed to the ceiling; beta's is
    # fresher and still on its declared band.
    _card("t_alpha_head", priority=790_000, seconds_ago=12 * HOUR, board="alpha")
    _card("t_beta_head", priority=890_000, seconds_ago=0, board="beta")

    with _open("alpha") as conn:
        alpha_key = kbd.head_of_line_priority(conn, now=NOW)
        alpha_row = kbd.spawnable_rows(conn, now=NOW)[0]
    with _open("beta") as conn:
        beta_key = kbd.head_of_line_priority(conn, now=NOW)

    # The key IS the key the row was ordered by.
    assert alpha_key == int(alpha_row["eff_priority"]) == 900_000
    assert beta_key == 890_000
    assert alpha_key > beta_key

    # Allocator, as ``tick_once`` now feeds it: one free slot, alpha visited first.
    cards = [(alpha_key, "alpha"), (beta_key, "beta")]
    assert kwd.host_budget_shares_by_priority(1, cards) == {"alpha": 1}

    # ... and with the DECLARED values it would have gone the other way, which is
    # exactly the defect this clause fixes.
    assert kwd.host_budget_shares_by_priority(1, [(790_000, "alpha"), (890_000, "beta")]) == {
        "beta": 1
    }


# ---------------------------------------------------------------------------
# 6. No write path persists an effective value
# ---------------------------------------------------------------------------


def test_no_write_path_persists_an_effective_value(kanban_home):
    """Effective values are COMPUTED ONLY — 900006 is outside the domain."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    with _open() as conn:
        _board(conn)
    waited = _card("t_waited", priority=890_000, seconds_ago=5 * HOUR)

    with _open() as conn:
        kbd._lane_rows(conn, "ready", now=NOW)
        kbd.spawnable_rows(conn, now=NOW)
        kb.list_tasks(conn, status="ready", now=NOW)
        kb.board_stats(conn)
        stored = conn.execute("SELECT priority FROM tasks WHERE id = ?", (waited,)).fetchone()
    assert int(stored["priority"]) == 890_000, "the declared value is what is stored"

    # Source-level: every priority write binds a PARAMETER — no UPDATE computes
    # or adds the credit into the column.
    source = Path(kb.__file__).read_text()
    writes = [
        line.strip() for line in source.splitlines() if "SET priority" in line
    ]
    assert writes, "expected the designation/gate doors to write priority"
    for write in writes:
        assert "SET priority = ?" in write, write


# ---------------------------------------------------------------------------
# 7. The starvation block: which BAND is not draining
# ---------------------------------------------------------------------------


def test_starvation_block_names_the_band_that_is_not_draining(kanban_home):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    with _open() as conn:
        _board(conn)
    for index in range(3):
        _card(f"t_top_{index}", priority=900_000, seconds_ago=60 * index)
    mid = _card("t_mid", priority=890_000, seconds_ago=5 * DAY)
    starved = _card("t_starved", priority=690_000, seconds_ago=20 * DAY)
    bottom = _card("t_bottom", priority=-1000, seconds_ago=HOUR)

    with _open() as conn:
        block = kb.starvation_block(conn, now=NOW)
        lane = _ids(kbd._lane_rows(conn, "ready", now=NOW))

    assert block["ready"] == 6
    # The oldest card by age is the 20-day 690000 card: it has climbed to the
    # ceiling and lands FIRST — before the fresh top band.
    assert block["oldest"]["task_id"] == starved
    assert block["oldest"]["priority"] == 690_000
    assert block["oldest"]["wait_seconds"] >= 20 * DAY - 5
    assert block["oldest"]["rank"] == lane.index(starved) + 1 == 1
    assert block["rank_fraction"] == round(1 / 6, 4)

    bands = {tier["band"]: tier for tier in block["tiers"]}
    assert bands[900_000]["ready"] == 3
    assert bands[890_000]["oldest_task_id"] == mid
    assert bands[690_000]["oldest_rank"] == lane.index(starved) + 1
    # The rank is the discriminator age alone cannot express: the one-hour-old
    # bottom-band card is LAST in dispatch order.
    assert bands[-1000]["oldest_task_id"] == bottom
    assert bands[-1000]["oldest_rank"] == len(lane) == 6
    assert bands[-1000]["oldest_wait_seconds"] < 2 * HOUR

    # Only the band past the declared bound is reported: the 5-day band and the
    # fresh top band are both inside it.
    assert block["starved_tiers"] == [690_000]


def test_starvation_block_is_empty_safe(kanban_home):
    """A board with nothing ready yields the block, never a KeyError."""
    from hermes_cli import kanban_db as kb

    with _open() as conn:
        _board(conn)
        stats = kb.board_stats(conn)
    assert stats["starvation"] == {
        "ready": 0,
        "oldest": {
            "task_id": None, "priority": None, "wait_seconds": None, "rank": None,
        },
        "rank_fraction": None,
        "tiers": [],
        "starved_tiers": [],
    }
