"""The ONE caller wired to the archived-key seam: the landing CARRIER path.

Ruling on ``t_2bc058fc``, leg B ``t_0f97637f``, item 4: the kernel seam is opt-in, and
exactly one caller is converted as proof -- the path the landing-carrier clause's own
remedy names, i.e. a ``kanban_create`` carrying ``landing-carrier:<owner>/<repo>#<N>``.
A carrier's identity is its landing, so an archived card holding that key is this
landing's own carrier and is REOPENED rather than twinned.

The boundary is asserted in the same breath: a NON-carrier key stays on the kernel
default, so the conversion is the carrier namespace and nothing else.
"""

from __future__ import annotations

import json

import pytest

CARRIER_KEY = "landing-carrier:yoyodine-industries/yaan-platform#821"
PLAIN_KEY = "sdlc-refusal:test-signature:lane-a"


@pytest.fixture
def door(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB",
                "HERMES_DELEGATED_CHILD_CONTEXT"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return kb, kt


def _file(kt, key, title):
    answer = json.loads(kt._handle_create(dict(
        title=title, assignee="platform-coder", idempotency_key=key)))
    assert answer.get("ok"), answer
    return answer


def _rows(kb, key):
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return conn.execute(
            "SELECT id, status FROM tasks WHERE idempotency_key = ? ORDER BY created_at",
            (key,)).fetchall()


def test_the_carrier_path_reopens_the_archived_carrier_instead_of_twinning_it(door):
    kb, kt = door
    first = _file(kt, CARRIER_KEY, "[land] yoyodine-industries/yaan-platform#821 for t_x")
    assert kb.archive_task.__module__  # the kernel owns the archive door
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert kb.archive_task(conn, first["task_id"]) is True

    second = _file(kt, CARRIER_KEY, "[land] yoyodine-industries/yaan-platform#821 for t_x")

    rows = _rows(kb, CARRIER_KEY)
    assert second["task_id"] == first["task_id"], "the carrier was TWINNED beside its tombstone"
    assert len(rows) == 1
    assert rows[0]["status"] != "archived"
    # The door SAYS what it did: a reopened carrier is not a silent re-file.
    assert second["archived_key_policy"] == "reopen"


def test_a_plain_key_still_mints_a_second_card_through_the_same_door(door):
    """The boundary: the conversion is the carrier namespace, NOT every keyed create."""
    kb, kt = door
    from hermes_cli import kanban_db_connect as kbc

    first = _file(kt, PLAIN_KEY, "the refusal")
    with kbc.connect_closing() as conn:
        assert kb.archive_task(conn, first["task_id"]) is True

    second = _file(kt, PLAIN_KEY, "the refusal, re-filed")

    rows = _rows(kb, PLAIN_KEY)
    assert second["task_id"] not in (None, first["task_id"])
    assert len(rows) == 2
    assert [r["status"] for r in rows].count("archived") == 1
    assert "archived_key_policy" not in second


def test_a_live_carrier_is_not_reopened_it_is_just_deduped(door):
    """A live carrier under the key answers the filing; nothing is reopened or inserted."""
    kb, kt = door
    first = _file(kt, CARRIER_KEY, "[land] yoyodine-industries/yaan-platform#821 for t_x")
    second = _file(kt, CARRIER_KEY, "[land] yoyodine-industries/yaan-platform#821 for t_x")
    rows = _rows(kb, CARRIER_KEY)
    assert second["task_id"] == first["task_id"]
    assert len(rows) == 1
