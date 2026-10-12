"""Nothing but the operator's asks and SEVs is admitted above the reserved tranche.

Operator ruling 2026-09-28 (card ``t_6ce41549``, design record platform-stl): the operator's
asks and SEVs hold the RESERVED TOP TRANCHÉ (``kanban_priority_policy``: 990000..999999), and
no row anywhere carries a value above the ceiling ``MAX_PRIORITY``.

Measured on the live estate before this guard (2026-09-28): 52 rows above the ceiling on
``defcon`` (47 at 1100000, 4 at 1090000, 1 at 1050000) and 1 on ``ops`` — 10 of them designated,
none carrying an ask stamp, i.e. plain lane work outranking the operator's asks in the only
place that decides: the dispatcher's pick order (``priority DESC, created_at ASC``). Both doors
were open on EVERY unwired board, and every board is unwired: the create door's clamp and the
re-rank door's refusal are both gated on a board's ``priority_policy``.

What these tests pin down:

* the create seam refuses an above-ceiling filing BEFORE any write (no row, no event) and the
  refusal text carries the standard and the three legitimate moves;
* the attempt is escalated as a public deviation row on the attempt's board, against the filing
  lane BY NAME, and repeats collapse onto that same row;
* the marker — the filing's own ask reference, the register's ``Operator-ask:`` stamp, or a
  DECLARED SEV1 line — admits the filing INTO the tranche at its top, never above it, with the
  pin recorded in the card's own ``created`` event;
* the re-rank door refuses a hand-lift above the ceiling on a card with no marker, and only
  that: a designated or stamped card may still be lifted;
* the repair pass lowers a row already above the ceiling into its class ceiling, records it on
  the card, is idempotent, and leaves a clean board alone;
* the ordering the ruling is about holds after the repair: the lane's own pick order puts the
  ask ahead of the card that used to outrank it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REGISTER = "t_fc615201"
ASK = "t_8ca4b5a0"
ASK_REF = f"{REGISTER}/{ASK}"
STAMP = f"Operator-ask: {ASK_REF}"

CEILING = 999999
TRANCHE_FLOOR = 990000
ORDINARY_MAX = 900000


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB (same shape as the sibling guards' suites)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "platform-worker")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # A dispatcher-spawned worker shell pins these; a test must not touch a live board.
    # ``HERMES_KANBAN_OPERATOR_ASK`` is the sharpest of them: the shell exports its own
    # ask (this session: ``t_fc615201/t_ecbfb34b``), ``_resolve_operator_ask`` honours it,
    # and every card the test creates then carries the marker — so the above-tranche
    # doors return early instead of refusing and the suite reads red for no reason.
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_OPERATOR_ASK",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_ADVISORY_SKILLS"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _rows_titled(conn, title: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE title = ?", (title,)
    ).fetchone()[0]


def _created_events(conn, title: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM task_events e JOIN tasks t ON t.id = e.task_id "
        "WHERE t.title = ? AND e.kind = 'created'", (title,)
    ).fetchone()[0]


def _priority_of(conn, title: str):
    row = conn.execute("SELECT priority FROM tasks WHERE title = ?", (title,)).fetchone()
    return None if row is None else int(row["priority"])


def _policy_record(conn, title: str) -> dict:
    import json

    payload = conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks t ON t.id = e.task_id "
        "WHERE t.title = ? AND e.kind = 'created'", (title,)
    ).fetchone()[0]
    data = json.loads(payload)
    return (data.get("priority_policy") or {})


# ---------------------------------------------------------------------------
# Door 1 — the create seam refuses, and creates nothing for the attempt
# ---------------------------------------------------------------------------


def test_above_ceiling_filing_is_refused_before_any_write(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.AboveTrancheRefused) as excinfo:
            kb.create_task(
                conn, title="lane work claiming the operator's band",
                assignee="platform-worker", created_by="platform-worker",
                priority=1100000,
            )
        # A ValueError, so every surface reports a validation refusal, not a crash.
        assert isinstance(excinfo.value, ValueError)
        assert _rows_titled(conn, "lane work claiming the operator's band") == 0
        assert _created_events(conn, "lane work claiming the operator's band") == 0
        # The structured facts a caller needs to fix the call.
        assert excinfo.value.attempted_priority == 1100000
        assert excinfo.value.ceiling == CEILING
        assert excinfo.value.lane == "platform-worker"


def test_the_refusal_carries_the_standard_and_the_three_moves(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.AboveTrancheRefused) as excinfo:
            kb.create_task(
                conn, title="quiet over-claim", assignee="platform-worker",
                created_by="platform-worker", priority=1000000,
            )
        message = str(excinfo.value)
        assert "above the reserved top tranche" in message
        assert "NOTHING WAS CREATED" in message
        # Move 1: file inside the domain.
        assert str(ORDINARY_MAX) in message
        # Move 2: carry the marker (ask reference / stamp / declared SEV1).
        assert "serves=" in message and "Operator-ask:" in message and "Severity: SEV1" in message
        # Move 3: the designation door for a card that must rank in the tranche.
        assert "designate" in message
        assert "operator ruling 2026-09-28" in message


def test_the_deviation_row_is_filed_against_the_filing_lane_by_name(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.AboveTrancheRefused) as excinfo:
            kb.create_task(
                conn, title="lane work claiming the operator's band",
                assignee="platform-worker", created_by="platform-worker",
                priority=1100000,
            )
        deviation = excinfo.value.deviation_task_id
        assert deviation, excinfo.value.deviation_error
        row = conn.execute(
            "SELECT assignee, created_by, priority, body, title FROM tasks WHERE id = ?",
            (deviation,),
        ).fetchone()
        assert row["assignee"] == "platform-worker"      # the lane, BY NAME
        assert row["created_by"] == kb.ABOVE_TRANCHE_GUARD_IDENTITY
        assert row["priority"] <= ORDINARY_MAX           # the record is filed in-band
        assert "1100000" in row["body"]
        assert "REFUSED" in row["body"]
        assert "lane work claiming the operator's band" in row["title"]


def test_a_repeat_attempt_collapses_onto_the_same_deviation_row(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        ids = []
        for _ in range(2):
            with pytest.raises(kb.AboveTrancheRefused) as excinfo:
                kb.create_task(
                    conn, title="repeated over-claim", assignee="platform-worker",
                    created_by="platform-worker", priority=1100000,
                )
            ids.append(excinfo.value.deviation_task_id)
        assert ids[0] == ids[1] and ids[0]
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_by = ?",
            (kb.ABOVE_TRANCHE_GUARD_IDENTITY,),
        ).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# The marker — an ask or a declared SEV1 is admitted INTO the tranche, never above it
# ---------------------------------------------------------------------------


def test_an_ask_reference_admits_the_filing_into_the_tranche(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="the operator's ask, filed hot", assignee="platform-worker",
            created_by="platform-worker", priority=1100000, serves=ASK_REF,
        )
        assert _priority_of(conn, "the operator's ask, filed hot") == CEILING
        record = _policy_record(conn, "the operator's ask, filed hot")
        assert record["above_tranche"]["requested"] == 1100000
        assert record["above_tranche"]["applied"] == CEILING
        assert record["above_tranche"]["marker"] == "operator-ask-ref:explicit"
        # The stamp the register writes is on the card, so the marker is self-describing.
        body = conn.execute("SELECT body FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        assert STAMP in body


def test_the_register_stamp_admits_a_filing_with_no_reference(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        kb.create_task(
            conn, title="stamped ask", body=STAMP, assignee="platform-worker",
            created_by="platform-worker", priority=1100000,
        )
        assert _priority_of(conn, "stamped ask") == CEILING
        assert _policy_record(conn, "stamped ask")["above_tranche"]["marker"] == \
            "operator-ask-ref:body"


def test_a_declared_sev1_admits_a_filing_and_a_bare_claim_does_not(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        kb.create_task(
            conn, title="gateway down for every lane",
            body="Severity: SEV1 - production is down",
            assignee="platform-worker", created_by="platform-worker", priority=1100000,
        )
        assert _priority_of(conn, "gateway down for every lane") == CEILING
        assert _policy_record(conn, "gateway down for every lane")["above_tranche"]["marker"] \
            == "sev1"

        # Unlabelled is not a declaration: SEV1 is declared on a line, never inferred.
        with pytest.raises(kb.AboveTrancheRefused):
            kb.create_task(
                conn, title="this is a sev1, trust me", body="sev1 sev1 sev1",
                assignee="platform-worker", created_by="platform-worker", priority=1100000,
            )


def test_the_guard_reads_the_requested_value_not_the_boards_clamped_output(
    kanban_home: Path,
) -> None:
    """A wired board's clamp must not silently absorb (and hide) an over-claim."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        with pytest.raises(kb.AboveTrancheRefused):
            kb.apply_above_tranche_guard(
                conn, 1100000, ORDINARY_MAX, board="defcon", lane="platform-worker",
                title="clamped to the ordinary edge by the board policy",
            )
        # The in-band path is untouched: applied value, no record, byte-identical event.
        assert kb.apply_above_tranche_guard(conn, 500000, 500000, board="defcon") == (500000, None)


def test_a_filing_at_the_tranche_floor_is_not_this_doors_business(kanban_home: Path) -> None:
    """The ceiling is what this guard owns; the band INSIDE the tranche is the domain door's
    (wired boards) and the fleet sweep's business - the boundary is stated, not implied."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        kb.create_task(
            conn, title="inside the tranche, unwired board", assignee="platform-worker",
            created_by="platform-worker", priority=TRANCHE_FLOOR,
        )
        assert _priority_of(conn, "inside the tranche, unwired board") == TRANCHE_FLOOR


# ---------------------------------------------------------------------------
# Door 2b — the re-rank seam (the door that created the class)
# ---------------------------------------------------------------------------


def test_a_hand_lift_above_the_ceiling_is_refused(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="lane work", assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        with pytest.raises(kb.AboveTrancheRefused):
            kb.edit_task(conn, tid, priority=1100000)
        assert _priority_of(conn, "lane work") == ORDINARY_MAX
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'reprioritized'",
            (tid,),
        ).fetchone()[0] == 0


def test_a_marked_card_may_still_be_lifted_and_a_plain_one_may_be_lowered(
    kanban_home: Path,
) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        ask = kb.create_task(
            conn, title="the ask", body=STAMP, assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        kb.edit_task(conn, ask, priority=1100000)          # marker: allowed
        assert _priority_of(conn, "the ask") == 1100000

        plain = kb.create_task(
            conn, title="plain", assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        kb.edit_task(conn, plain, priority=ORDINARY_MAX - 1)
        assert _priority_of(conn, "plain") == ORDINARY_MAX - 1


# ---------------------------------------------------------------------------
# Door 3 — the repair pass: a row already above the ceiling is LOWERED
# ---------------------------------------------------------------------------


def test_the_repair_pass_lowers_each_class_into_its_ceiling(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        plain = kb.create_task(
            conn, title="plain lane work", assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        stamped = kb.create_task(
            conn, title="stamped ask", body=STAMP, assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        designated = kb.create_task(
            conn, title="designated", assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        # Simulate the legacy rows: the designation is real, then the hand-lift door of the day
        # moved the row above the ceiling (the live defcon history exactly: designation ledger at
        # 999000, the row itself at 1100000).
        kb.designate_priority(conn, designated, reason="operator ask", authority="default")
        for tid, value in ((plain, 1100000), (stamped, 1100000), (designated, 1100000)):
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (value, tid))

        ledger = kb.demote_above_tranche(conn)   # the connection's own board, as the tick calls it
        moved = {row["task_id"]: row for row in ledger}
        assert moved[plain]["now"] == ORDINARY_MAX and moved[plain]["marker"] == ""
        assert moved[stamped]["now"] == CEILING
        # The marker is the STORAGE GUARD's reading of the row (``tranche_entitlement``): the
        # pass must land a value the guard accepts, so its answer names the evidence the guard
        # can see. It used to be "operator-ask-ref:body" - the wider resolver's label, resolved
        # through the ask machinery rather than read off the row - and that difference is what
        # made the pass's own write refusable (2026-10-01, ``defcon``).
        assert moved[stamped]["marker"] == "operator-ask-stamp"
        assert moved[designated]["now"] == CEILING
        assert moved[designated]["marker"] == "designation"
        assert _priority_of(conn, "plain lane work") == ORDINARY_MAX
        assert _priority_of(conn, "stamped ask") == CEILING

        # The repair is recorded on the card, so its lane sees it instead of finding out by order.
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'priority_demoted'",
            (plain,),
        ).fetchone()
        assert event is not None
        import json

        payload = json.loads(event["payload"])
        assert payload["from"] == 1100000 and payload["to"] == ORDINARY_MAX
        assert "reserved tranche" in payload["reason"]


def test_the_repair_pass_is_idempotent_and_inert_on_a_clean_board(kanban_home: Path) -> None:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect() as conn:
        kb.create_task(
            conn, title="clean", assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        assert kb.demote_above_tranche(conn, board="defcon") == []
        assert _priority_of(conn, "clean") == ORDINARY_MAX
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'priority_demoted'"
        ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The ordering the ruling is about
# ---------------------------------------------------------------------------


def test_the_lane_picks_the_ask_ahead_of_the_card_that_used_to_outrank_it(
    kanban_home: Path,
) -> None:
    """The DoD's ordering clause, at the seam that decides it: after the repair, the ask is the
    lane's head of line and the plain card — which HELD the higher number, 1100000 — is not."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as dispatch

    with kbc.connect() as conn:
        plain = kb.create_task(
            conn, title="plain lane work", assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        ask = kb.create_task(
            conn, title="the ask", body=STAMP, assignee="platform-worker",
            created_by="platform-worker", priority=ORDINARY_MAX,
        )
        # The legacy state: the plain card held the higher number and was picked first.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 1100000 WHERE id = ?", (plain,))
            conn.execute("UPDATE tasks SET priority = 1100000 WHERE id = ?", (ask,))
        pre = [row["id"] for row in dispatch._lane_rows(conn, "ready")]
        assert pre.index(plain) < pre.index(ask), pre       # the defect, reproduced

        kb.demote_above_tranche(conn, board="defcon")
        post = [row["id"] for row in dispatch._lane_rows(conn, "ready")]
        assert post.index(ask) < post.index(plain), post    # the ruling holds
        # ``head_of_line_priority`` gates on the host's own profile registry, which the isolated
        # test home does not have - the lane's own row order above is the property under test.
